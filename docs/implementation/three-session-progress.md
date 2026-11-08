# Three-session implementation ledger

Status: 0.6.5 (88) candidate implementation complete; installation and real Workbuddy 30-minute acceptance pending. 0.6.4 remains the previous single-session release.

## Decisions

- Workbuddy's installed harness supports PreToolUse `hookSpecificOutput.modifiedInput`
  for command hooks (and `updatedInput` in its functional hook layer). The MCP path
  applies modified input before dispatch. Use both fields with identical values.
- The adapter issues a random, single-use token bound to the exact tool name and
  arguments. Session/request IDs come from the host hook context, never model input.
- A shared stdio connector routes each verified host session to an isolated Python
  bridge process. Each has private native handles and task/browser files. A root
  registry enforces three slots and target arbitration across connectors.
- The native broker retains preview/privacy ownership. Each bridge connection has
  an isolated native execution process, capture state and scheduler. Killing one
  execution worker must not abort the broker or other clients.
- Browser channels use separate observers, queues, bindings, creation acknowledgments
  and deduplication records. Native-host dispatch validates the channel against the
  live registry before accessing its files.

## Figma

File: Tr6hAfLgUAulbXkagRyX3S
Session: de80883a-1543-4529-ad7f-906e6cbbf4bd
Existing preview board: 113:1074
Session tab component set: 135:1092 (Live 134:1084, Selected 135:1084, Waiting 135:1088)

- P0.a–f: existing single-card geometry and WB tokens inspected. New deck required.
- P1.a–h: reuse existing 51 variables, two color modes, seven text styles and popover
  effect. No duplicate palette. Figma Noto Sans SC maps to native system UI font.
- P2.a–c: extend current Overview/Components/Handoff structure without moving old boards.
- P3: session tab made with text property and three states; deck screens 137:1074 and browser panel session selection 141:1086 complete.
- P4: screenshots and text bounds reviewed; existing radius/12 reused for native preview geometry. No old boards moved or overlapped.

## Validation evidence

- Python MCP: 281 tests passed; browser extension: 59 tests passed; packaging: 34 tests passed; targeted native Swift: 66 tests passed.
- Signed native three-client integration passed: killing A leaves B/C handles alive and streams advancing with the same generation.
- Signed synthetic deck probe passed: 15/5/5 fps settings, unchanged stream generations on six switches, no foreground or physical-mouse change.
- Diagnostic artifacts: dist/three-session-probe-signing-report.json and /private/tmp/quietbridge-native-three.log, /private/tmp/quietbridge-deck-probe.log.

## Integration acceptance outstanding

- Real Workbuddy browser/native cross-operation isolation, stop/cancel/timeout and clipboard interruption.
- Real host preview follow/drag/resize ergonomics; synthetic stream/deck and geometry tests cover the component layer.
- Real Workbuddy three-session 30-minute acceptance remains a user-host integration gate.

## Package

- Generated dist/quietbridge-0.6.5.pkg (build 88, 2.49 MiB).
- Unpacked app/helper/plugin signatures and supplied profiles verified; staged source and extension 0.4.1 matched.
- Packaged preflight and MCP initialization/list passed without user-state writes; packaged native isolated permission/watchdog smoke passed (legacy helper case skipped).
- dist/quietbridge-0.6.5-verification.json records checksum and container signing status. No installation performed.
