"""Upgrade contracts: old callers reach the same guarded implementation."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import compatibility
import server


class CompatibilityTests(unittest.TestCase):
    def test_existing_environment_settings_work_and_explicit_new_values_win(self):
        with patch.dict(os.environ, {'CUA_READ_ONLY': '1'}, clear=True):
            self.assertEqual(compatibility.environment('QUIETBRIDGE_READ_ONLY'), '1')
            os.environ['QUIETBRIDGE_READ_ONLY'] = ''
            self.assertEqual(compatibility.environment('QUIETBRIDGE_READ_ONLY'), '')
            self.assertEqual(compatibility.environment('QUIETBRIDGE_ALLOWLIST', 'unset'), 'unset')

    def test_upgrade_reuses_existing_stable_connector_and_unifies_browser_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.assertEqual(compatibility.runtime_root(home).name, 'QuietBridge')
            legacy = home / 'Library/Application Support/workbuddy-cua'
            legacy.mkdir(parents=True)
            self.assertEqual(compatibility.runtime_root(home), legacy)
            old_browser = home / 'Library/Application Support/WorkbuddyExtension'
            (old_browser / 'cua-helper-config').mkdir(parents=True)
            root, host, bridge, state = compatibility.browser_layout(home)
            # One live browser runtime: the app runtime root, not the legacy
            # WorkbuddyExtension directory. Legacy dirs stay migration sources.
            self.assertEqual(root, compatibility.runtime_root(home))
            self.assertEqual(host, root / 'extension/native-host')
            self.assertEqual(bridge, host)
            self.assertEqual(state, root)
            self.assertIn(old_browser / 'cua-helper-config',
                          compatibility.legacy_browser_state_dirs(home))

    def test_old_tool_calls_enter_the_canonical_dispatch_and_recovery_path(self):
        for tool in server.TOOLS:
            current = tool['name']
            self.assertTrue(current.startswith('quietbridge_'))
            old = 'cua_' + current[len('quietbridge_'):]
            result = {'isError': True, 'errorCode': 'PERMISSION_DENIED',
                      'content': [{'type': 'text', 'text': 'Permission required'}]}
            with self.subTest(tool=current), patch.object(server, '_call_tool', return_value=result) as dispatch, \
                    patch.object(server, 'operation_log'), patch.object(server, 'ROUTING_ENABLED', False):
                reply = server.handle({'id': 1, 'method': 'tools/call',
                    'params': {'name': old, 'arguments': {'action': 'status'}}})['result']
                dispatch.assert_called_once_with(current, {'action': 'status'})
                self.assertEqual(reply['structuredContent']['tool'], current)
                self.assertTrue(reply['structuredContent']['recovery']['waitForUser'])
                self.assertFalse(reply['structuredContent']['recovery']['allowAlternativeTools'])
