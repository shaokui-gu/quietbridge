import json
import unittest
from unittest.mock import patch

import server
from recovery import policy


class RecoveryTests(unittest.TestCase):
    def test_popup_guard_blockers_require_user_guidance_and_never_auto_refresh(self):
        for code in ('POPUP_GUARD_LEGACY', 'POPUP_GUARD_CONFLICT'):
            result = policy(code, 'unknown')
            self.assertEqual(result['status'], 'user_action_required')
            self.assertTrue(result['notifyUser'])
            self.assertTrue(result['waitForUser'])
            self.assertFalse(result['allowAlternativeTools'])
            self.assertFalse(result['replaySideEffects'])
        self.assertIn('不自动刷新', policy('POPUP_GUARD_LEGACY', 'unknown')['message'])

    def test_connected_observer_failure_has_bounded_status_checks_before_notifying_user(self):
        recovery = policy(None, 'not_requested', {'nativeConnection': 'connected',
            'executionPolicy': {'waitingReason': 'observer_not_ready'}})
        self.assertEqual(recovery['reason'], 'BROWSER_OBSERVER_NOT_READY')
        self.assertEqual(recovery['maxAutomaticChecks'], 2)
        self.assertEqual(recovery['onExhausted'], 'notify_user_and_wait')
        self.assertFalse(recovery['allowAlternativeTools'])
        self.assertFalse(recovery['replaySideEffects'])

    def test_browser_rejection_and_observation_timeout_have_distinct_recovery(self):
        for code in ('BROWSER_REQUEST_REJECTED', 'BROWSER_STORAGE_UNAVAILABLE', 'INVALID_BROWSER_RESULT'):
            with self.subTest(code=code):
                recovery = policy(code, 'unknown')
                self.assertTrue(recovery['notifyUser'])
                self.assertTrue(recovery['waitForUser'])
                self.assertFalse(recovery['replaySideEffects'])
        recovery = policy('BROWSER_OBSERVATION_TIMEOUT', 'unknown')
        self.assertEqual(recovery['status'], 'wait_required')
        self.assertEqual(recovery['maxAutomaticChecks'], 2)
        self.assertFalse(recovery['allowAlternativeTools'])

    def test_stale_document_keeps_original_binding_and_does_not_request_assignment(self):
        for code in ('STALE_DOCUMENT', 'STALE_TARGET'):
            recovery = policy(code, 'unknown')
            self.assertEqual(recovery['action'], 'inspect_bound_document')
            self.assertFalse(recovery['replaySideEffects'])
            self.assertIn('不要每次导航都 candidates+assign', recovery['message'])

    def result(self, code, records=(), payload=None):
        def dispatch(*args):
            server.CALL_RECORDS.extend(records)
            return {'content': [{'type': 'text', 'text': json.dumps(payload or {})}],
                    **({'isError': True, 'errorCode': code} if code else {})}
        with patch.object(server, '_call_tool', side_effect=dispatch):
            return server.call_tool('quietbridge_browser_events', {'action': 'status'})

    def test_disabled_app_notifies_and_waits_before_any_input(self):
        with patch.object(server, 'control_snapshot', return_value=None):
            result = server.call_tool('quietbridge_click', {})
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

    def test_channel_starting_requires_bounded_observation_instead_of_extension_reload(self):
        data = self.result(None, payload={'nativeConnection': 'host_unavailable',
            'extensionConnection': 'connected', 'connectionScope': 'task_channel',
            'taskChannelReady': False, 'executionPolicy': {'waitingReason': 'browser_channel_starting'}})['structuredContent']
        self.assertEqual(data['recovery']['reason'], 'BROWSER_CHANNEL_STARTING')
        self.assertEqual(data['recovery']['status'], 'wait_required')
        self.assertEqual(data['recovery']['maxAutomaticChecks'], 2)
        self.assertFalse(data['recovery']['allowAlternativeTools'])
        self.assertNotEqual(data['recovery'].get('action'), 'connect_extension')

    def test_async_browser_restriction_is_a_user_blocker_not_a_successful_action(self):
        result = self.result(None, payload={'status': 'error', 'operation': 'click',
            'errorCode': 'BROWSER_DEBUGGER_RESTRICTED', 'error': 'restricted'})
        data = result['structuredContent']
        self.assertEqual(data['errorCode'], 'BROWSER_DEBUGGER_RESTRICTED')
        self.assertEqual(data['page']['status'], 'error')
        self.assertEqual(data['recovery']['action'], 'resolve_extension_conflict')
        self.assertEqual(data['suggestedNext'], 'notify_user_and_wait')
        self.assertFalse(data['recovery']['allowAlternativeTools'])
        self.assertFalse(data['recovery']['replaySideEffects'])
        self.assertIn('recovery', json.loads(result['content'][0]['text']))

    def test_permission_status_carries_authorization_guidance(self):
        permissions = {'permissions': {'accessibility': False, 'screenRecording': True}}
        with patch.object(server, '_call_tool', return_value={'content': [{'type': 'text', 'text': 'accessibility=False'}],
                                                            'permissionStatus': permissions}):
            result = server.call_tool('quietbridge_permissions', {'action': 'status'})
        self.assertEqual(result['structuredContent']['recovery']['action'], 'grant_permission')
        self.assertEqual(result['structuredContent']['permissionStatus'], permissions)
        self.assertIn('accessibility=False', result['content'][0]['text'])

    def test_stale_snapshot_refreshes_without_replay_or_user_restart(self):
        data = self.result('STALE_SNAPSHOT')['structuredContent']
        self.assertEqual(data['recovery']['status'], 'revalidate_required')
        self.assertFalse(data['recovery']['replaySideEffects'])
        self.assertNotEqual(data['suggestedNext'], 'notify_user_and_wait')

    def test_stale_app_limits_reselection_and_does_not_infer_upgrade_or_restart(self):
        recovery = self.result('STALE_APP')['structuredContent']['recovery']
        self.assertEqual(recovery['status'], 'revalidate_required')
        self.assertEqual(recovery['maxAutomaticChecks'], 2)
        self.assertEqual(recovery['onExhausted'], 'notify_user_and_wait')
        self.assertFalse(recovery['allowAlternativeTools'])
        self.assertFalse(recovery['replaySideEffects'])
        self.assertIn('不自动关闭或重启应用', recovery['message'])
        self.assertIn('不等于已确认应用升级或退出', recovery['message'])

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

    def test_browser_failures_do_not_recommend_an_immediate_managed_profile_fallback(self):
        for code in ('BROWSER_DEBUGGER_RESTRICTED', 'BACKGROUND_COMMAND_TIMEOUT', 'TASK_SCOPE_REQUIRED'):
            data = policy(code, 'unknown')
            self.assertTrue(data['waitForUser'])
            self.assertFalse(data['allowAlternativeTools'])
            self.assertFalse(data['replaySideEffects'])
        self.assertIn('需用户', policy('BROWSER_DEBUGGER_RESTRICTED', 'unknown')['message'])

    def test_permission_report_includes_unlock_method_and_socket_readiness(self):
        privacy = {'lockedUse': {'available': True, 'socketStarted': False,
                                 'manualSuppression': True, 'lastUnlockMethod': 'manual'}}
        responses = {
            'permissionStatus': {'launchApplication': {}, 'helper': {'pid': 5}},
            'getCapabilities': {'sessionPrivacy': privacy, 'permissions': {}, 'platform': 'macos'},
        }
        with patch.object(server, 'call_helper', side_effect=lambda method, *args, **kwargs: responses[method]), \
             patch.object(server, 'browser_channel_state', return_value=True):
            result = server.tool_permissions({'action': 'status'})
        report = result['content'][0]['text']
        self.assertIn('manualSuppression=True', report)
        self.assertIn('unlockMethod=manual', report)
        self.assertIn('socketStarted=False', report)

    def test_manual_authentication_preserves_the_existing_task(self):
        data = policy('MANUAL_AUTHENTICATION_REQUIRED', 'not_dispatched')
        self.assertEqual(data['action'], 'finish_manual_unlock')
        self.assertIn('保留原任务和目标绑定', data['message'])
        self.assertFalse(data['replaySideEffects'])
        self.assertFalse(data['allowAlternativeTools'])

    def test_every_tool_carries_recovery_guidance_even_without_initialize_instructions(self):
        for tool in server.TOOLS:
            self.assertIn('user_action_required', tool['description'])
