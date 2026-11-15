# QuietBridge for DeepSeek Harness

This product composes the shared QuietBridge MCP service with
`adapters/deepseek_harness`. It reuses the same signed QuietBridge application
and browser extension as the Workbuddy connector.

`product.json` is internal release metadata, not a claimed DeepSeek Harness
marketplace manifest. Do not publish it as a verified host manifest.

## What the adapter delivers

- A DeepSeek Harness **bundle** (`adapters/deepseek_harness/bundle`): a Host-only
  plugin with no dependencies on Harness-internal packages. It is installed into
  the Harness profile through the host's own `plugin_manager` `install_bundle`
  flow.
- One connector per Harness Agent: the plugin starts
  `python3 -B <bridge>/server.py` with the same stdio protocol the Workbuddy
  connector uses. Each connector carries the trusted Agent identity supplied by
  the Host plugin (`QUIETBRIDGE_HOST_SESSION_ID`, `QUIETBRIDGE_HOST_AGENT_ID`),
  not model-declared names.
- Host lifecycle adaptation: turn start/end and Agent disposal are written to
  the host-neutral lifecycle store so stopped turns cannot dispatch new input,
  while task receipts still attribute window leases and preview ownership to the
  Harness session.
- `quietbridge_*` tools become per-Agent tools in the Harness tool list. The
  browser extension, macOS helper, permissions and recovery semantics are
  shared with the existing connector; this product adds no second capture,
  input or authorization implementation.

## Build and install

The installer never writes `~/.dsh`. Rendering writes only the requested output
directory:

```sh
# Preview what would be rendered (no writes)
python3 install.py --dsh-bundle /path/to/quietbridge-dsh --dsh-helper '/Applications/QuietBridge.app/Contents/MacOS/quietbridge-native'

# Render the installable bundle
python3 install.py --install --dsh-bundle /path/to/quietbridge-dsh --dsh-helper '/Applications/QuietBridge.app/Contents/MacOS/quietbridge-native'
```

Then install the rendered directory into the Harness profile that should use it.
This step is intentionally explicit because it changes that profile; the official
CLI performs the same link dependency, bundle selection and patch application as
the in-agent `plugin_manager install_bundle` flow:

```sh
dsh plugin --profile <profile> add /path/to/quietbridge-dsh
```

If the QuietBridge PKG is installed, the deployment step already rendered a
ready-to-install bundle from the verified release:
`~/Library/Application Support/QuietBridge/releases/<digest>/dsh-bundle`
(recorded as `dshBundle.installCommand` in the deployment result; rendering
failure never blocks the Workbuddy install). Install it with the same command.

After the profile reloads, each new Harness Agent gets its own connector and the
`quietbridge_*` tools. Start the helper/permissions, ask for a background window
binding, and confirm the lifecycle store records that session's turns and task
receipts before claiming support.

The repository also ships an isolated reproduction check that never touches the
user's `~/.dsh`:

```sh
env -u DSH_HOME -u PYTHONPATH python3 tools/dsh_isolated_check.py
```

It creates a temporary `web` profile, installs the rendered bundle through
`dsh plugin add`, boots the real host, and asserts 19 per-Agent tools, the turn
lifecycle, routing allow/deny and the disposal tombstone. It skips cleanly when
the Harness app is absent and never starts the real helper.

## Verified scope and current limits

Verified in this repository round (source-level):

- Host contract facts, bundle/patch shape, Agent/session/turn events and tool
  registration rules are recorded in
  [`dsh-host-contract.zh.md`](../../docs/architecture/dsh-host-contract.zh.md).
- Bundles render deterministically; Python adapter, per-Agent routing and the
  connector wire protocol have unit tests.
- **Isolated real-host verification passed**: in a temporary `DSH_HOME`/profile,
  `dsh plugin add` installed the rendered bundle, the host booted, the plugin
  activated, one Agent got 19 `quietbridge_*` tools from its own connector, a
  real turn's start/end reached the lifecycle store, host-context routing allowed
  a live-turn call and rejected a stopped-turn call, and Agent disposal wrote the
  tombstone. Evidence:
  [`2026-11-15-dsh-isolated-verification.zh.md`](../../docs/implementation/2026-11-15-dsh-isolated-verification.zh.md).

Not verified, and not claimed:

- No installation into the user's real `desktop`/custom profile and no
  model-driven end-to-end tool-call run were performed. Installation,
  enablement and trust remain explicit user actions.
- The signed helper's real execution and TCC attribution when launched by the
  Harness process is not verified; the isolated check used a missing helper path.
- DSH preview host binding is not implemented yet; the native side still selects
  preview hosts from its historical host logic. Do not claim DSH preview support.
- Multiple concurrent Harness Agents are not accepted as verified; the shared
  capacity limit stays at three sessions and further sessions are refused.

See `adapters/deepseek_harness/adapter.json` for the machine-readable status and
remaining blocking requirements.
