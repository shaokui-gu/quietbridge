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

Then install the rendered directory through the Harness profile that should use
it (this step is intentionally explicit; it changes the profile):

```
plugin_manager action=install_bundle target=/path/to/quietbridge-dsh
```

After the profile reloads, each new Harness Agent gets its own connector and the
`quietbridge_*` tools. Verify a real session before claiming support: start the
helper/permissions, ask for a background window binding, and confirm the
lifecycle store records that session's turns and task receipts.

## Verified scope and current limits

Verified in this repository round (source-level):

- Host contract facts, bundle/patch shape, Agent/session/turn events and tool
  registration rules are recorded in
  [`dsh-host-contract.zh.md`](../../docs/architecture/dsh-host-contract.zh.md).
- Bundles render deterministically; Python adapter, per-Agent routing and the
  connector wire protocol have unit tests.

Not verified, and not claimed:

- No live profile installation and no end-to-end Harness run were performed.
  Installation, enablement and trust remain explicit user actions.
- The signed helper's TCC attribution when launched by the Harness process is
  not verified.
- DSH preview host binding is not implemented yet; the native side still selects
  preview hosts from its historical host logic. Do not claim DSH preview support.
- Multiple concurrent Harness Agents are not accepted as verified; the shared
  capacity limit stays at three sessions and further sessions are refused.

See `adapters/deepseek_harness/adapter.json` for the machine-readable status and
remaining blocking requirements.
