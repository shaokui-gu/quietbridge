"""QuietBridge signing identities and explicit legacy upgrade boundaries.

Legacy identifiers may be accepted only when inspecting existing signed code.
New releases and their executor/plugin trust checks use the current identities.
Storage paths, authorization mechanisms and stable connectors remain compatible.
"""
import os
from pathlib import Path

LEGACY_SERVER_NAMES = ('workbuddy-cua', 'cua')
LEGACY_APP_NAMES = ('Workbuddy CUA.app', 'workbuddy-cua.app')
MENU_IDENTIFIER = 'com.quietbridge.app'
HELPER_IDENTIFIER = 'com.quietbridge.helper'
PLUGIN_IDENTIFIER = 'com.quietbridge.AuthorizationPlugin'
LEGACY_MENU_IDENTIFIER = 'com.workbuddy.cua.menubar'
LEGACY_HELPER_IDENTIFIER = 'com.workbuddy.cua.helper'
LEGACY_PLUGIN_IDENTIFIER = 'com.workbuddy.cua.AuthorizationPlugin'
NATIVE_HOST_NAME = 'com.cua.browser_observer'
# Previously shipped stable transports. Preserve their bytes as well as their
# paths so a cosmetic rename does not deliberately invalidate host trust.
PUBLISHED_CONNECTOR_DIGESTS = frozenset((
    'abcf2cbdf8c248ff14b7a0808abf083d69f96732aff8e4eb17b83d375bc69b1d',
    '9d97cef4928cd45e5811526e3e2c22c2e58f0bbe23daa7764846af7df00796fb',
))


def environment(name, default=None):
    """New environment keys win; deployed CUA_* settings remain supported."""
    if name in os.environ:
        return os.environ[name]
    legacy = name.replace('QUIETBRIDGE_', 'CUA_', 1)
    return os.environ.get(legacy, default)


def runtime_root(home=None):
    base = Path(home or Path.home()) / 'Library/Application Support'
    legacy = base / 'workbuddy-cua'
    return legacy if legacy.exists() else base / 'QuietBridge'


def browser_runtime_root(home=None):
    """The one browser runtime root, inside the QuietBridge app runtime.

    Chrome's native host, browser task state and the Harness connector must all
    read and write the same directory. Legacy WorkbuddyExtension /
    QuietBridgeBrowser storage is only a migration source; it must never stay
    registered as a second live host.
    """
    return runtime_root(home)


def legacy_browser_state_dirs(home=None):
    base = Path(home or Path.home()) / 'Library/Application Support'
    return (base / 'WorkbuddyExtension' / 'cua-helper-config',
            base / 'QuietBridgeBrowser' / 'quietbridge-config')


def browser_layout(home=None):
    root = browser_runtime_root(home)
    host = root / 'extension' / 'native-host'
    # Host, bridge modules and task state live together so the Chrome native
    # host and the connector cannot drift into two state directories.
    return (root, host, host, root)


def application_runtime():
    # This kernel-lock namespace must agree with the signed native application.
    return Path('/tmp/workbuddy-cua-' + str(os.getuid()))


def canonical_tool(name):
    return 'quietbridge_' + name[4:] if isinstance(name, str) and name.startswith('cua_') else name
