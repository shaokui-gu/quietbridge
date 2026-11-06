# macOS installation package

Build the Swift release executable and the signed authorization plug-in, then:

```sh
env CLANG_MODULE_CACHE_PATH=/private/tmp/cua-ui-module-cache \
    SWIFT_MODULECACHE_PATH=/private/tmp/cua-ui-module-cache \
    swift build --package-path cua-helper-build --configuration release --disable-sandbox
python3 packaging/macos/build.py --version 0.5.27 --build 55 \
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

`deploy.py` runs as the desktop user. It stages and verifies the menu app, bridge
and extension, checks the deployed MCP initialize response, and backs up changed
user files with mode 0600 before switching. It preserves unrelated MCP servers,
extra connection settings and recognised legacy connection keys. A competing
configuration edit aborts the switch instead of overwriting the edit.

The LaunchAgent points to a verified versioned copy of the menu app, but does not
start it at login. The product archive always shows the native installation
choices: the application is required and “安装完成后立即运行” is optional and
unchecked by default. A scripts-only component opens the app after deployment
only when selected. Otherwise the user can open it from Applications.
Deployment failures restore user configuration, native-host
registration, the extension directory and the previous LaunchAgent.
The journal stays under `~/Library/Application Support/workbuddy-cua/transactions`.
If automatic recovery was interrupted, double-click
`/Library/Application Support/workbuddy-cua/恢复安装.command` before retrying.
Recovery preserves intervening user edits and reports conflicts.

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
