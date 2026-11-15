# QuietBridge for Workbuddy

This product composes the shared QuietBridge MCP service with
`adapters/workbuddy`. The macOS installer deploys the stable connector, merges
owned MCP configuration and installs host lifecycle hooks transactionally.

`product.json` is internal release metadata, not a claimed Workbuddy marketplace
manifest. A market submission must follow the verified marketplace schema and
must not include another native application or a fork of the engine.

One app and browser extension serve this integration. Existing owned connector
keys and stable paths are retained during upgrades; fresh installations use
`quietbridge` and publish `quietbridge_*` tools.

For a scoped preference to use QuietBridge for browser and desktop UI tasks,
see [WorkBuddy tool selection guidance](tool-preference.zh.md). MCP discovery text
helps the host select tools; it does not enforce a host-wide priority or change
the person's instructions or other connectors.
