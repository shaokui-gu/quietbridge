# DeepSeek Harness adapter

Reserved integration boundary for QuietBridge for DeepSeek Harness. This adapter
is not executable or ready for publication. No host API, identifier or market
manifest format has been assumed.

Before implementation, verify the official plugin and transport contracts, then
provide trusted host/agent/execution identities, cancellation events, permission
recovery guidance and preview ownership. Reuse `services/mcp` and `apps/macos`;
do not fork their input, capture or authorization implementations.

The broker and per-agent workers described in
[`quietbridge.zh.md`](../../docs/architecture/quietbridge.zh.md) are prerequisites
for safe parallel operation across hosts.
