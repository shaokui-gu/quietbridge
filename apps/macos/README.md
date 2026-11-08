---
description: "Native macOS helper protocol, input semantics, shared-service lifecycle and verification commands."
kind: "package-reference"
---

# QuietBridge native helper

English | [中文](README.zh.md)

## Summary

This macOS 14+ JSON-lines helper uses Swift 6, AppKit, Accessibility, CoreGraphics and ScreenCaptureKit. Windows support is in development. It adapts selected MIT-licensed Cua Driver behavior without loading an external driver. `upstream.json` records the fixed upstream version, adapted Swift files and local modules; `LICENSE.quietbridge-driver` preserves the MIT notice.

## Table of Contents

- [Build and service](#build-and-service)
- [Request protocol](#request-protocol)
- [Observation and input](#observation-and-input)
- [Verification](#verification)

-----

<a id="build-and-service"></a>
## Build and service

Build with `swift build --package-path apps/macos --configuration release`. The binary is `.build/release/quietbridge-native` beneath this directory. The Host runs `--ensure-shared` as a managed startup command; the serving process then belongs to its connected clients and exits after the last connection finishes cleanup. The `.build/` directory is not committed.

The shared service uses an owner-only Unix socket under `/tmp/quietbridge-<uid>`, keyed by the executable SHA-256. A version lock elects one process; an additional real-executable-path lock excludes a different build at the same path. The first frame is protocol-1 `hello` with the runtime key and this connection’s byte, AX, operation, screenshot, event-spacing and cleanup limits. `ready` reports the verified runtime key, PID and instance ID. Every connection owns an independent AXWorker. The stdin/stdout entry is a diagnostic proxy to this service.

Open `QuietBridge.app` before using the service. The menu application holds an exclusive kernel lock with a fresh session ID; workers require that same live session. “退出应用” revokes it, cancels native work, releases previews and power leases, and stops the helper. A crash also releases the lock. A stale lock file cannot enable work, and reopening the app cannot reactivate old worker sessions. The bridge and browser extension enforce this lifecycle too; connectors never start the helper while the app is closed.

First launch opens a separate desktop wizard: capabilities, Accessibility, Screen Recording, then completion. Its branded step rail and native light/dark content follow the Figma design; progress and completion reflect actual grants. It explains each permission before a deliberate authorization click requests consent and opens its Privacy & Security page. Startup and polling only preflight. Dismissing or postponing the wizard is supported and does not repeat the welcome screen on each launch. The panel's attached System Permissions page shows status and reopens the matching wizard step.

The menu retains a connection to the same signed helper used for desktop execution, and shows the system-reported responsible application. Missing or unknown permissions close both the effective API switches and their visual state: native observation/input and locked use require both grants; preview requires Screen Recording. Clicking a closed switch opens the first missing permission's explanation. A grant never overrides a manual opt-out. Browser permissions stay in the extension. Status refreshes every three seconds and on app activation; follow macOS's reopen instruction after a Screen Recording grant when required.

AppKit runs on the main event loop. Blocking socket accepts and handshakes use background queues; server callbacks explicitly carry `@MainActor @Sendable` isolation and weak ownership. Shutdown releases accepted connections even when their handshake is incomplete. Blocking AX and input use each connection’s serial actor; all connections share a FIFO for complete desktop operations. Socket writes do not hold that operation slot. A request cancellation waits for native work to stop; it does not cancel the connection’s lifetime latch. Screenshot timeout ends only the request. An unfinished system capture retains the single capture slot until its real callback arrives, rejects new captures with CAPTURE_BUSY and leaves AX/input available. Only an operation or cleanup that cannot stop can invalidate the shared process. The proxy accepts `--max-request-bytes`, `--max-response-bytes`, `--ax-timeout-ms`, `--timeout-ms`, `--screenshot-timeout-ms`, `--shutdown-grace-ms` and `--text-event-interval-ms`; the Host sets the same limits in hello. An AX deadline bounds an individual system call. Cancellation can follow partial input, and uncertain input is never replayed automatically.

Task resources belong to the acquiring socket. Disconnect cancels its native work and revokes only its preview and protection. An independent 120-second activity lease expires idle resources; ordinary status queries do not renew it. Expired sockets cannot resume input (TASK_INTERRUPTED), and other sockets cannot renew or release the owner’s resources. Protection release retains the existing system-lock handoff; already delivered input cannot be undone.


-----

<a id="request-protocol"></a>
## Request protocol

Operation frames remain `{ "id": "opaque request id", "method": "method name", "params": {} }`. The matching reply contains exactly one of `result` or `error: {code, message}`; malformed envelopes return `INVALID_REQUEST` with a null ID. A connection sends at most one operation at a time. `{ "type": "cancel", "id": "request id" }` cancels that request; a late cancel produces no extra reply. `protocol.schema.json` defines operation fields.

Screenshot diagnostics use `type: "diagnostic"` frames on the originating connection. Records contain requestId, stage, elapsedMs since receipt and optional errorCode, without window titles, AX text or image bytes. The stdin/stdout proxy forwards them to stderr with `[quietbridge-screenshot]`; operation replies remain on stdout. The Host retains a bounded diagnostic buffer and includes it in screenshot failures.

Successful `activate` and `performSecondaryAction` AXRaise results include `foreground` alongside `{dispatched: true, verified: false}`. It records `requestId`, `operation`, `targetPid`, nullable `targetWindowId`, `activationRequested`, `raiseRequested`, and immediate `before`/`after` observations. `frontPid` comes from WindowServer; `focusedWindowId` is the target app's AX-focused window; `frontWindowId` and `frontWindowPid` identify the first visible normal window. Unknown values are null. Observations are sequential, not atomic or proof of causality. Tool-owned requests use the logged tool call ID. Failed operations retain the existing error response and may have partial effects.

| Method | Result |
| --- | --- |
| `getCapabilities` | `{platform: "macos", permissions: {accessibility, screenRecording}, sessionLocked, methods, coordinateSpace: "window-points", browser: false}`. Permission values are boolean preflight observations; no system prompt opens. |
| `listApps` | Array of `{pid, name, bundleId}` for running regular applications. |
| `findApps` | Installed matches `{id, displayName, path, isRunning, pid?}` from standard application directories or an absolute application path; discovery never launches them. |
| `getApp` | `{appHandle, pid, name, bundleId, authorization}` selected by exact name, bundle ID, decimal PID or application path. A stopped selection has `pid: null` and waits for Host consent. Ambiguous names fail. |
| `launchApp` | Host-only continuation after consent; starts the selected application without requesting activation, waits for launch completion and binds the handle to its verified process identity. |
| `listWindows` | Owned normal windows with `{windowId, pid, appName, title, bounds, isOnScreen, onCurrentSpace}`. Space membership can be null. |
| `getState` | `{appHandle, pid, windowId, focusedWindowId, snapshotId, tree, elements, windows, truncated}`. `windowId` is null for an app-wide observation. Elements include index, role, title, value, description, identifier, bounds, actions, and enabled state. Element bounds are null or [x, y, width, height] in screen points; window input uses local points. These structured element diagnostics are retained by native checks and omitted by the model tool projection. Unavailable optional descriptive properties are null, and an unavailable action list is empty; permission, communication and window-discovery failures still reject the observation. |
| `hover`, `click`, `setValue`, `selectText`, `typeText`, `paste`, `pressKey`, `scroll`, `drag` | `{dispatched: true, verified: false}` records native dispatch. Read fresh state to verify the intended application change. |
| `screenshot` | `{mimeType: "image/png", data, pixelWidth, pixelHeight, bounds, coordinateSpace: "window-points", windowId}`. PNG pixel dimensions can differ from window point dimensions on Retina displays. |

Every app-targeted operation requires `appHandle` from `getApp` in the same helper. The helper pins the PID and kernel process start time, and invalidates the handle and all of its snapshots after process exit or PID reuse. Calling `getApp` can acquire the replacement process without reactivating old handles. A random per-helper namespace prevents handles from a terminated helper matching those of its replacement. Repeated acquisition of the same live process shares its handle. A post-launch inspection failure invalidates the pending handle and preserves the original error. It does not terminate a process already started; launchApp cannot reuse the failed handle.

Stable error codes are `INVALID_REQUEST` (malformed envelope), `INVALID_ARGUMENT` (invalid parameters), `UNSUPPORTED` (unknown operation), `APP_UNAVAILABLE`, `APP_PATH_UNAVAILABLE`, `PROCESS_INSPECTION_FAILED`, `AMBIGUOUS_APP`, `STALE_APP`, `STALE_SNAPSHOT`, `WINDOW_UNAVAILABLE`, `WINDOW_CHANGED`, `PERMISSION_DENIED`, `FOREGROUND_REQUIRED` (hover target is not the unobscured foreground window), `PANEL_NOT_READY` (system panel recipient unavailable before input), and `NATIVE_ERROR` (remaining operating-system failures). An error never promises that a partially dispatched input was rolled back. The host must not retry input automatically.

Application selection returns an authorization record to the Host: a SHA-256 digest of the case-sensitive bundle identifier prefixed with `macos:bundle-id:`, display name, optional executable path, and persistable flag. All installations with that identifier share consent, including unsigned and ad-hoc-signed applications. Without a nonempty bundle identifier, both running and stopped applications use SHA-256 of `macos:executable-path:` followed by the symlink-resolved executable path and remain eligible for saved consent. A running bundle identifier remains usable when the process path query fails; the record omits executablePath. Available paths must still match the selection. Without either identity, APP_PATH_UNAVAILABLE reports the path query cause, including its errno, before any target operation. PROCESS_INSPECTION_FAILED means the process instance could not be verified; only ESRCH from process inspection is reported as a missing process. Neither failure triggers an automatic retry. Process IDs and start times validate live handles but do not enter the saved identity. The Host strips this record from model results and obtains human consent before launch, observation or input. This pipe protocol is trusted Host transport, not a sandbox against a separately authorized shell process.

The checked-in `protocol.schema.json` and `Sources/QuietBridgeNative/NativeProtocol.generated.swift` define the native protocol; Swift tests verify field and enum handling. The upstream TypeScript generator is not part of this repository. Model-submittable parameters exclude Host resource bounds and the private launch continuation.


-----

<a id="observation-and-input"></a>
## Observation and input

`getState` optionally scopes AX traversal to an owned window or nested sheet `windowId`. A new or failed refresh invalidates the previous snapshot for that app/window scope, while other window scopes remain usable. When an owned CG window has no matching AX surface, getState returns a new snapshot with empty tree/elements and no truncation; its coordinates and screenshots remain available. It rechecks process lifetime, window ownership, the unlocked session and Accessibility permission after traversal. Window-discovery APIDisabled and communication errors remain failures; unsupported or absent optional AX attributes can produce an empty observation. Missing or foreign-owned windows never produce an empty success. An AX element invalidated during traversal rejects the whole read with `STALE_SNAPSHOT`; no partial snapshot is published. Callers may read again after the application changes, without repeating input. Element actions require the exact snapshot ID. Native references never cross JSON. An optional snapshot ID on coordinate actions must belong to that window or an app-wide observation. Window-list refresh releases snapshots for closed windows. Native process identity, snapshot generation, and window ownership checks do not lock the desktop against simultaneous user or application changes.

Element clicks without holdMs prefer advertised AX actions and otherwise use pointer delivery. Coordinate click, scroll and drag validate window ownership and bounds, then use background PID/window-directed primitives. Each event uses SkyLight when available, otherwise the public PID-targeted API. A posted event is never repeated through the other API. Coordinates are macOS points from the top-left of the entire window, including its title bar. Positive `deltaLinesY` reveals content above; negative reveals below. Positive `deltaLinesX` reveals content left; negative reveals right. Each axis accepts an integer from -10 to 10, and at least one must be nonzero. The helper sends line-unit scroll events directly. Click and drag accept left/right/middle buttons and command/control/option/shift/fn modifiers. Only left clicks support double-click. Modified element clicks use pointer delivery. Drag uses one button press/move/release in one window; callers explicitly supply durationMs (1..5000) and steps (1..240). The movement delay budget is divided with fractional precision across the segments; fixed press/release settling and scheduler latency are additional. No implicit fallback sends global desktop input.

`click.holdMs` is an optional nonnegative integer in milliseconds per press; zero adds no hold delay. Each connection supplies `maxClickHoldMs` to bound explicit holds. The diagnostic helper option `--max-click-hold-ms` defaults to 5000. It bypasses AX actions and uses the element center or supplied coordinates. Omission retains ordinary click timing. The complete gesture holds the request scheduler slot; cancellation, timeout and disconnect interrupt the hold and post the pending mouse-up before releasing that slot. Received events remain effective, and forced process termination cannot guarantee cleanup.

Capture and pointer delivery read CG bounds before and after an exact-window AX frame query. Movement during the query returns WINDOW_CHANGED. Valid AX/CG frames must have equal outward-rounded integral point rectangles; a known mismatch returns WINDOW_CHANGED before mouse movement, priming or button delivery. The helper preserves both coordinate sources rather than substituting AX geometry for CG geometry. Missing or unusable AX geometry remains unknown, while AX communication, permission and invalid-element errors propagate. Capture without Accessibility permission retains its CG/SCK checks. Advertised AX actions bypass this pointer preflight; an AX action error does not trigger pointer input. These reads do not lock the window against later movement, and no geometry check activates or raises it.

`typeText` and `pressKey` target the selected process's current focused control, not an exact window. Key chords send PID-targeted modifier transitions before the base key and release modifiers in reverse order; these events do not use the global HID queue. Background pointer actions may change that keyboard focus. Keypad names such as KP_0 and KP_Enter preserve their physical key codes. Text injection sends Unicode characters individually and does not use the clipboard. LF, CR and CRLF send one Return; tabs send Tab. Applications apply their normal editing behavior, including indentation and focus navigation. Each down/up event is followed by the configured text interval; dispatch completion does not confirm that the receiving application consumed every event. Cancellation can interrupt text or a gesture after partial input; it cannot undo received events. Very long text can exceed the host's operation deadline. Validate observed application state before deciding how to continue.

The responsible host/helper needs macOS Accessibility permission for AX state and input, and Screen Recording permission for screenshots. Capability preflight and permission errors do not grant permissions. Capture refuses a window that moves or resizes while the image is being read. Unassociated system panels and accessory-only applications are not selectable.

Host-only permission controls are `permissionStatus {}`, `requestPermission {permission}`, and `openPermissionSettings {permission}`, where permission is `accessibility` or `screenRecording`. They are excluded from model tools and from `getCapabilities.methods`. Permission status includes observed helper/parent process paths; these paths do not identify the TCC responsible application. The operating-system prompt names the application and asks for the user’s decision. The helper identifies the responsible process with the optional libSystem SPI `responsibility_get_pid_responsible_for_pid`, then captures its application name and icon before publishing readiness. The function is resolved dynamically, following the same signature used by [Chromium](https://chromium.googlesource.com/chromium/src/+/refs/tags/134.0.6998.28/base/process/process_info_mac.mm). If the symbol, process, or application metadata is unavailable, attribution stays unknown; it is not inferred from the nearest GUI ancestor. Permission checks remain independent of this metadata.

Observation reads each non-layout node’s descriptive properties and children through `AXUIElementCopyMultipleAttributeValues`. Per-attribute errors retain the individual-read rules; unsupported batch APIs use individual reads. Values are retained only during that observation, and input reads live attributes. Application state can change between nodes; a batch does not make the whole tree atomic.

System file-panel keyboard routing resolves the remote recipient through the selected app’s focused AX sheet. Only Apple’s Open and Save Panel Service is accepted, and its focused ancestry must still contain the identified remote window. Element pointer fallbacks resolve an app-owned window or sheet through AX parents when AXWindow is unavailable. Ordinary clicks retain AX action priority over pointer input; visual click markers need no system-cursor movement.

Request modifier arrays accept command/control/option/shift/fn. The object API normalizes key-chord aliases before dispatch.

`selectText` requires a fresh element snapshot and writable AXSelectedTextRange. It matches one literal occurrence, narrowed by immediately adjacent prefix/suffix, and selects text or places the cursor before/after it. Matching and reported ranges use UTF-16 offsets. Ambiguous or missing text fails; the helper rechecks the value and selected range without rewriting content. AX text includes available writable, disabled, selected and selection-range information; AXAccess owns indices and native references while AXTextFormatter renders the observed values.

`paste` publishes one plain-text or HTML clipboard format, dispatches the application's enabled standard Paste menu command, and waits for a clipboard data request. It then restores the preserved formats only while its ownership marker and change count still match. The Host serializes its pastes across helpers with a file lock. maxClipboardBytes bounds both input and preserved contents; unsupported menus and oversized data fail before dispatch. A later copy is preserved. Cancellation restores owned clipboard data when possible; forced termination cannot guarantee restoration, and a data request does not prove the target completed editing. After dispatch, `pasteReadTimeoutMs` bounds waiting for clipboard consumption. Expiry returns `PASTE_NOT_CONSUMED`, closes the promised data provider, and restores the original clipboard only while still owned. The failure does not prove the app will never process its queued Paste command; inspect the target before deciding whether to retry.

`hover` accepts an observed element index or a window-local point and never presses a mouse button. Disabled controls may be hovered. No request completion, AX read or screenshot restores the previous point; subsequent hover calls can enter revealed content. Click sends its button events at the target without an off-window priming click. Individual requests are serialized, but no application is reserved between calls. Other input or application changes can interrupt the interaction.

`hover` first uses a same-window element or ancestor advertising both `AXShowAlternateUI` and `AXShowDefaultUI`. Revealed descendants retain the parent UI; leaving restores it once. An AX action failure propagates without trying another input route. Without an applicable AX route, hover moves the visible system cursor through the system input queue after checking the foreground PID and AppKit mouse hit testing at the point. Input-transparent windows and transparent regions do not block the target. A failed check returns `FOREGROUND_REQUIRED` without moving the cursor. The result identifies `hoverRoute` as `accessibility` or `system-pointer` and reports `systemPointerMoved`. Hover does not activate, raise or restore focus. Other input or window changes can race the check and dispatch.


-----

<a id="verification"></a>
## Verification

Run `python3 apps/macos/tests/test_swift.py` from the repository root. The tests
cover protocol admission, ownership, task cleanup, background-input routing,
preview state and panel layout with isolated fixtures. They do not replace
signed application, real permission attribution or host integration tests.

`apps/macos/tests/test_permission_service.py` provides an opt-in helper lifetime
smoke test using its own application lock and temporary Unix sockets. It never
requests a permission grant. Consult its environment options before enabling it.

The full architecture and planned parallel-runtime requirements are documented
in [the overall plan](../../docs/architecture/quietbridge.zh.md).
