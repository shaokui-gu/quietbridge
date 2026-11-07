# macOS installation package

Build the Swift release executable and the signed authorization plug-in, then:

```sh
env CLANG_MODULE_CACHE_PATH=/private/tmp/cua-ui-module-cache \
    SWIFT_MODULECACHE_PATH=/private/tmp/cua-ui-module-cache \
    swift build --package-path cua-helper-build --configuration release --disable-sandbox
python3 packaging/macos/build.py --version 0.5.49 --build 77 \
  --identity 'Developer ID Application: shaokui gu (62RNP54C47)'
```

Building writes only to `dist/`. It does not install applications, launch services,
change user configuration or modify authorization rules. The resulting PKG is
currently unsigned and unnotarized; the executable apps and optional plug-in are
signed with Developer ID Application.

The package requires a signed-in desktop user, Apple Silicon, macOS 14+ and a
working native arm64 Python 3.9+. Preinstall checks the environment, connection
ownership, symlinks and unfinished installation journals before replacing payload
files. Python is not bundled or automatically downloaded. macOS permissions and
connector trust still require the user's confirmation.

The menu app keeps identifier `com.workbuddy.cua.menubar`. A separate app signed
as `com.workbuddy.cua.helper` is installed into an immutable, root-owned release
under `/Library/Application Support/WorkBuddyCUAHelper/releases/`. The plug-in's
strict identifier/team checks remain enabled. Both apps carry the same version.
Bundle relocation is disabled.

The user-visible bundle is installed as `/Applications/Workbuddy CUA.app`. During
an upgrade, the preinstall check verifies the old `workbuddy-cua.app` bundle's
identifier and Developer ID signature before moving it to the new visible name.
The helper bundle and executable names remain unchanged.
When stopping an old menu process, the installer accepts either system bundle
name in its original launch arguments: macOS updates the process executable path
after a directory rename while retaining the original arguments. User ownership,
the allowed executable directories and menu-only arguments are still checked.

`deploy.py` runs as the desktop user. It stages and verifies the menu app, bridge
and extension, checks the deployed MCP initialize response, and backs up changed
user files with mode 0600 before switching. It preserves unrelated MCP servers,
extra connection settings and recognised legacy connection keys. A competing
configuration edit aborts the switch instead of overwriting the edit.

The LaunchAgent points to a verified versioned copy of the menu app, but does not
start it at login. The product archive always shows the native installation
choices: the application is required and “安装完成后立即运行” is optional and
unchecked by default. A scripts-only component opens the app after deployment
when selected. First installs and upgrades from a stopped app keep it stopped
otherwise; upgrades preserve a running app by starting and verifying the new
menu release. Failure restores the prior files and running service state.
On first launch, a separate desktop wizard with a branded step rail and native
light/dark content introduces capabilities and explains
Accessibility and Screen Recording before the user requests either permission.
Missing grants keep the corresponding capability switches off; clicking one
reopens its explanation step. The attached System Permissions page provides
status and setup entry points. No TCC prompt opens during installation or preflight.
The panel's “退出应用” ends the application and revokes desktop and browser
control. Connectors remain installed but cannot run tools or restart the helper
until the person opens the app again. Reopening creates a fresh session and does
not resume old browser commands.
Deployment failures restore user configuration, native-host
registration, the extension directory and the previous LaunchAgent.
The journal stays under `~/Library/Application Support/workbuddy-cua/transactions`.
If automatic recovery was interrupted, double-click
`/Library/Application Support/workbuddy-cua/恢复安装.command` before retrying.
Recovery preserves intervening user edits and reports conflicts.

From 0.5.49 the MCP transport uses the fixed user entrypoint
`~/Library/Application Support/workbuddy-cua/connector/connector.py`.
The transaction publishes `active.json` last. The connected process checks it
between complete requests, loads the new bridge and helper path, and emits
`notifications/tools/list_changed` without dropping stdio. In-flight requests
finish on their original release; no input is replayed. The first tool request
after switching returns `CONNECTOR_UPDATED` without dispatch so the agent must
revalidate its original target and restore shared-window observation. Reload
failure retains the healthy prior bridge; transaction rollback switches the
manifest back. The connector writes its generation and PID to `status.json`;
the installer reports `reloaded` only after acknowledging the new generation.
An idle/disconnected host reports `pending_connection` and uses the current
manifest when next connected. Disabled connectors stay disabled, and unrelated
servers, tool policies and host trust records are never edited.

Migration from an older direct `server.py` connection requires one host refresh
and any host trust confirmation. Current Workbuddy exposes refresh internally
but has no verified external installer API; the installer reports
`host_refresh_required` instead of claiming a reconnect. It does not terminate
Workbuddy or change its private trust database. Subsequent upgrades keep the
same command/arguments/environment and require no manual off/on.

This transaction does **not** roll back system files installed by PackageKit. In
particular, when upgrading from a release whose old LaunchAgent points directly
to `/Applications/workbuddy-cua.app`, restoring that agent cannot restore its
previous executable. Versioned menu releases prevent that issue on later upgrades.
A failed postinstall is reported as failure; retry the package after resolving it.

The browser host is registered automatically. Chrome still requires the person
to load the unpacked extension and grant site access. The browser task panel's
“安装扩展…” button shows those steps inside the app, with actions to open
Chrome's extension manager, copy the deployed directory and reveal it in Finder.
Installation does not open Terminal, a local HTTP server or a completion dialog.

The optional authorization plug-in is carried as a signed bundle with its manager
under `/Library/Application Support/workbuddy-cua/plugin/`. The main package
never activates it. The menu panel opens the packaged manager rather than asking
for a source checkout. Installation, update, enable, status and restore/uninstall
are available there. System changes require the user's menu selection and local
administrator authentication. An update requires a recognised committed policy,
keeps the original authorization backup, and restores its binary/receipt on
failure. Unknown or unfinished authorization transactions must be inspected and
restored, rather than overwritten. After updating a loaded plug-in, log out and
back in. Installing the plug-in never switches on the menu's lock-screen capability.

Validation (no real system installation):

```sh
python3 -B -m unittest discover -s packaging/macos -p 'test_*.py'
python3 -B -m unittest discover -s cua-auth-plugin/tests -p 'test_install.py'
python3 -B -m unittest discover -s cua-mcp-bridge -p 'test_installer.py'
python3 -B -m unittest discover -s cua-mcp-bridge -p 'test_browser_events.py'
python3 cua-helper-build/tests/test_swift.py --disable-sandbox --filter MenuBar
```

Exercise the real permission executor and its background session watchdog in an
isolated runtime directory without requesting consent or starting the menu app:

```sh
CUA_PERMISSION_SMOKE_EXECUTABLE="$PWD/cua-helper-build/.build/release/workbuddy-cua-native" \
  python3 -B cua-helper-build/tests/test_permission_service.py
```

0.5.50 includes task cancellation and lease cleanup. The stable connector now installs its sibling `stdio_transport.py` transactionally before publishing `active.json`; releases include `task_lifecycle.py`. A connector process still executing the 0.5.49 entrypoint can hot-load the bridge and its watchdog, but concurrent cancellation reading takes effect on its next normal reconnection. Crash detection and the 120-second idle fallback remain available without forcing a host restart.

0.5.51 verifies live legacy bridge processes independently of the configuration file. A cached old server requires Workbuddy to reload its configuration; a normal connector reconnect can reuse the cached startup command. The installer reports that explicitly instead of treating a previous configuration migration as a successful reconnect. After commit it retires only previous signed CUA helper versions owned by the desktop user; a versioned application session prevents those older helpers from reconnecting. Workbuddy and target applications are never terminated by this cleanup.

0.5.52 (build 80) packages the task cleanup behavior confirmed by the user: stopping the session closes its shared preview. Legacy migration guidance now instructs the user to save work and fully restart Workbuddy once so it reads the stable connector entrypoint.
