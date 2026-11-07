import json
import unittest
from unittest.mock import patch

import server
from recovery import policy


class RecoveryTests(unittest.TestCase):
    def result(self, code, records=(), payload=None):
        def dispatch(*args):
            server.CALL_RECORDS.extend(records)
            return {'content': [{'type': 'text', 'text': json.dumps(payload or {})}],
                    **({'isError': True, 'errorCode': code} if code else {})}
        with patch.object(server, '_call_tool', side_effect=dispatch):
            return server.call_tool('cua_browser_events', {'action': 'status'})

    def test_disabled_app_notifies_and_waits_before_any_input(self):
        with patch.object(server, 'control_snapshot', return_value=None):
            result = server.call_tool('cua_click', {})
        data = result['structuredContent']
        self.assertEqual(data['execution']['status'], 'not_dispatched')
        self.assertEqual(data['recovery']['action'], 'open_app')
        self.assertEqual(data['suggestedNext'], 'notify_user_and_wait')
        self.assertFalse(data['recovery']['allowAlternativeTools'])
        self.assertTrue(data['recovery']['waitForUser'])
        self.assertIn('请打开', result['content'][-1]['text'])

    def test_timeout_preserves_uncertainty_while_asking_user(self):
        result = self.result('HELPER_TIMEOUT', [{'method': 'click', 'dispatched': True}])
        data = result['structuredContent']
        self.assertEqual(data['execution']['status'], 'unknown')
        self.assertTrue(data['recovery']['executionUncertain'])
        self.assertFalse(data['recovery']['replaySideEffects'])
        self.assertEqual(data['suggestedNext'], 'notify_user_and_wait')

    def test_successful_status_read_still_exposes_user_blocker(self):
        result = self.result(None, payload={'executionPolicy': {'waitingReason': 'permission_missing'}})
        data = result['structuredContent']
        self.assertTrue(data['ok'])  # Status read succeeded; permission is a separate blocker.
        self.assertEqual(data['execution']['status'], 'not_requested')
        self.assertEqual(data['recovery']['reason'], 'BROWSER_PERMISSION_MISSING')
        self.assertEqual(data['suggestedNext'], 'notify_user_and_wait')

    def test_no_candidates_does_not_invent_a_permission_failure(self):
        result = self.result(None, payload={'nativeConnection': 'connected', 'globalPermission': True,
                                          'candidates': [], 'refreshPending': True})
        self.assertNotIn('recovery', result['structuredContent'])

    def test_permission_status_carries_authorization_guidance(self):
        permissions = {'permissions': {'accessibility': False, 'screenRecording': True}}
        with patch.object(server, '_call_tool', return_value={'content': [{'type': 'text', 'text': 'accessibility=False'}],
                                                            'permissionStatus': permissions}):
            result = server.call_tool('cua_permissions', {'action': 'status'})
        self.assertEqual(result['structuredContent']['recovery']['action'], 'grant_permission')
        self.assertEqual(result['structuredContent']['permissionStatus'], permissions)
        self.assertIn('accessibility=False', result['content'][0]['text'])

    def test_stale_snapshot_refreshes_without_replay_or_user_restart(self):
        data = self.result('STALE_SNAPSHOT')['structuredContent']
        self.assertEqual(data['recovery']['status'], 'revalidate_required')
        self.assertFalse(data['recovery']['replaySideEffects'])
        self.assertNotEqual(data['suggestedNext'], 'notify_user_and_wait')

    def test_busy_has_bounded_checks_then_user_notification(self):
        data = policy('SHARE_NOT_READY', 'not_dispatched')
        self.assertEqual(data['maxAutomaticChecks'], 2)
        self.assertEqual(data['onExhausted'], 'notify_user_and_wait')

    def test_interruptions_require_user_continuation_without_old_task_replay(self):
        for code in ('TASK_CANCELLED', 'TASK_INTERRUPTED', 'TASK_IN_USE'):
            data = policy(code, 'unknown')
            self.assertTrue(data['waitForUser'])
            self.assertFalse(data['replaySideEffects'])
            self.assertTrue(data['executionUncertain'])

    def test_target_mismatch_does_not_assume_stale_binding(self):
        data = policy('TARGET_MISMATCH', 'not_dispatched')
        self.assertEqual(data['action'], 'inspect_task_and_original_target')
        self.assertIn('不代表旧任务已失效', data['message'])

    def test_every_tool_carries_recovery_guidance_even_without_initialize_instructions(self):
        for tool in server.TOOLS:
            self.assertIn('user_action_required', tool['description'])
