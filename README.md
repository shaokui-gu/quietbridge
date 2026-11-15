# QuietBridge

QuietBridge connects AI agents to background application and browser operations
on macOS. It preserves the user's foreground application, physical pointer and
current Space. Native operations use an explicitly shared target window;
browser operations use the extension and never fall back to desktop input.

The product has one macOS application, one browser extension and separate host
integrations. Workbuddy is implemented. DeepSeek Harness now has a source-level
adapter and bundle (not installed or end-to-end verified); concurrent agent
workers and DSH preview host binding are still planned. The current runtime
still has one active task and preview.

## Repository

| Directory | Responsibility |
| --- | --- |
| `apps/macos` | QuietBridge app, menu panel, permissions, native executor and preview |
| `services/mcp` | Host-neutral MCP tools, task state, browser messaging and recovery |
| `extensions/chromium` | QuietBridge Browser: fixed-tab CDP operations and observation |
| `system/authorization-plugin` | Optional signed authorization plugin for Locked use |
| `adapters/workbuddy` | Workbuddy configuration and trusted lifecycle hooks |
| `adapters/deepseek_harness` | DSH contract, per-Agent connector adapter and installable Harness bundle |
| `products` | Separate host product metadata and publication boundaries |
| `packaging/macos` | Shared application PKG assembly, upgrade and recovery |
| `tools` | Shared release layout and developer checks |
| `docs/architecture` | Overall architecture, migration and parallel-runtime plan |

`project.json` defines product names and implementation status. The folder
containing a checkout is arbitrary; no developer-specific absolute path is
required to build the project.

## Build and verify

Requires Apple Silicon, macOS 14+, Swift 6, Python 3.9+ and Node.js for extension
tests. Run from the repository root:

```sh
swift build --package-path apps/macos --configuration release
python3 apps/macos/tests/test_swift.py
python3 -m unittest discover -s services/mcp -p 'test_*.py' -q
python3 -m unittest discover -s packaging/macos -p 'test_*.py' -q
python3 -m unittest discover -s system/authorization-plugin/tests -p 'test_install.py' -q
node --test extensions/chromium/*.test.mjs
node --test adapters/deepseek_harness/bundle/test/*.test.mjs
```

The executable is `apps/macos/.build/release/quietbridge-native`. Build the
optional authorization plugin using its Makefile and the developer's signing
identity. Build a release installer with:

```sh
python3 packaging/macos/build.py --version VERSION --build BUILD --identity 'Developer ID Application: …'
```

The result is `dist/quietbridge-VERSION.pkg`, containing `QuietBridge.app`.
Application signing and installer signing/notarization are separate release
steps; building this archive does not claim that its outer PKG is signed.

Render an installable DeepSeek Harness bundle without touching the DSH profile:

```sh
python3 install.py --install --dsh-bundle /path/to/rendered-bundle \
  --dsh-helper '/Applications/QuietBridge.app/Contents/MacOS/quietbridge-native'
```

Installing that directory into a Harness profile stays an explicit
`plugin_manager install_bundle` action. See
[`products/deepseek-harness-plugin`](products/deepseek-harness-plugin/README.md)
and [`dsh-host-contract.zh.md`](docs/architecture/dsh-host-contract.zh.md).

## Architecture and compatibility

The complete plan and implementation status live in
[`docs/architecture/quietbridge.zh.md`](docs/architecture/quietbridge.zh.md).
The detailed parallel-runtime specification is
[`multi-agent-runtime.zh.md`](docs/architecture/multi-agent-runtime.zh.md).

New tools use `quietbridge_*`. Existing `cua_*` calls remain accepted as aliases;
existing owned connection keys, environment settings and stable connector paths
remain supported. Bundle identifiers are now `com.quietbridge.app`,
`com.quietbridge.helper` and `com.quietbridge.AuthorizationPlugin`; the Developer
ID team remains unchanged. macOS permissions must be checked and granted again
for the new identity. Existing authorization plug-ins need an explicit update
through the app's manager and a logout/login; the main PKG does not activate or
rewrite system authorization policy. Legacy storage and mechanism identifiers
remain supported and are documented in the architecture file.

Third-party attribution remains in `apps/macos/upstream.json` and
`apps/macos/LICENSE.cua-driver`.
