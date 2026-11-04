#!/usr/bin/env python3
"""Switch only the installed remote branch to Apple's privileged plug-in host."""
import copy
import plistlib
import uuid
import install


def migrate():
    install.require_root('privileged authorization host migration')
    state = plistlib.loads(install.STATE_FILE.read_bytes())
    if state.get('phase') != 'committed' or state.get('protocolVersion') != 3:
        raise RuntimeError('Unexpected installation state')
    install.verify_bundle(install.INSTALLED_BUNDLE)
    actual = {right: plistlib.loads(install.authorization_read(right))
              for right in (install.RIGHT, install.STOCK_RIGHT, install.REMOTE_RIGHT)}
    for right, policy in actual.items():
        if not install.policy_matches(policy, state['actualPolicies'][right]):
            raise RuntimeError('Policy changed externally: ' + right)
    expected_primary = {'class': 'rule', 'k-of-n': 1,
                        'rule': [install.REMOTE_RIGHT, install.STOCK_RIGHT]}
    if not install.policy_matches(actual[install.RIGHT], expected_primary):
        raise RuntimeError('Password fallback branch is not intact')
    old = actual[install.REMOTE_RIGHT]
    if old.get('mechanisms') != ['WorkBuddyCUAAuthorizationPlugin:allow']:
        raise RuntimeError('Remote mechanism is not the expected old host')
    backup = install.STATE_DIRECTORY / ('before-privileged-' + uuid.uuid4().hex + '.plist')
    install.atomic_plist(backup, {'state': state, 'policies': actual}, 0o600)
    disabled = copy.deepcopy(state)
    disabled['enabled'] = False
    install.atomic_plist(install.STATE_FILE, disabled, 0o644)
    try:
        desired = dict(old)
        desired['mechanisms'] = [install.PLUGIN_MECHANISM]
        new = install.write_verified(install.REMOTE_RIGHT, desired)
        for right in (install.RIGHT, install.STOCK_RIGHT):
            if not install.policy_matches(plistlib.loads(install.authorization_read(right)), actual[right]):
                raise RuntimeError('Fallback policy changed during migration')
        updated = copy.deepcopy(state)
        updated['actualPolicies'][install.REMOTE_RIGHT] = new
        updated['expectedPolicies'][install.REMOTE_RIGHT]['mechanisms'] = [install.PLUGIN_MECHANISM]
        updated['mechanism'] = install.PLUGIN_MECHANISM
        updated['privilegedMigrationBackup'] = str(backup)
        install.atomic_plist(install.STATE_FILE, updated, 0o644)
    except Exception:
        install.write_verified(install.REMOTE_RIGHT, old)
        install.atomic_plist(install.STATE_FILE, state, 0o644)
        raise
    print('Remote mechanism migrated; primary and password fallback policies unchanged; backup: ' + str(backup))


if __name__ == '__main__':
    migrate()
