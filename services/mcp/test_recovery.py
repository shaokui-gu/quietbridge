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
        self.assertEqual(recovery['onExhausted'], 'ask_user_choice')
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
        self.assertEqual(data['suggestedNext'], 'ask_user_choice')
        self.assertFalse(data['recovery']['allowAlternativeTools'])
        self.assertTrue(data['recovery']['waitForUser'])
        self.assertIn('请打开', result['content'][-1]['text'])

    def test_timeout_preserves_uncertainty_while_asking_user(self):
        result = self.result('HELPER_TIMEOUT', [{'method': 'click', 'dispatched': True}])
        data = result['structuredContent']
        self.assertEqual(data['execution']['status'], 'unknown')
        self.assertTrue(data['recovery']['executionUncertain'])
        self.assertFalse(data['recovery']['replaySideEffects'])
        self.assertEqual(data['suggestedNext'], 'ask_user_choice')

    def test_successful_status_read_still_exposes_user_blocker(self):
        result = self.result(None, payload={'executionPolicy': {'waitingReason': 'permission_missing'}})
        data = result['structuredContent']
        self.assertTrue(data['ok'])  # Status read succeeded; permission is a separate blocker.
        self.assertEqual(data['execution']['status'], 'not_requested')
        self.assertEqual(data['recovery']['reason'], 'BROWSER_PERMISSION_MISSING')
        self.assertEqual(data['suggestedNext'], 'ask_user_choice')

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
        self.assertEqual(data['suggestedNext'], 'ask_user_choice')
        self.assertFalse(data['recovery']['allowAlternativeTools'])
        self.assertFalse(data['recovery']['replaySideEffects'])
        self.assertIn('recovery', json.loads(result['content'][0]['text']))

    def test_debugger_restriction_and_existing_client_have_distinct_recovery(self):
        restricted = policy('BROWSER_DEBUGGER_RESTRICTED', 'unknown')
        occupied = policy('BROWSER_DEBUGGER_IN_USE', 'unknown')
        self.assertNotEqual(restricted['action'], occupied['action'])
        self.assertIn('JSON 查看器', restricted['message'])
        self.assertIn('不保证', restricted['message'])
        self.assertIn('不强制 detach', occupied['message'])
        self.assertTrue(restricted['waitForUser'] and occupied['waitForUser'])
        self.assertFalse(restricted['replaySideEffects'] or occupied['replaySideEffects'])

    def test_proven_pre_input_rejection_is_not_reported_as_an_unknown_click(self):
        payload = {'status': 'error', 'operation': 'navigate', 'errorCode': 'BROWSER_DEBUGGER_RESTRICTED',
                   'debuggerFailure': {'inputDispatched': False}}
        with patch.object(server, '_call_tool', return_value={'content': [{'type': 'text', 'text': json.dumps(payload)}]}):
            reply = server.call_tool('quietbridge_browser_events', {'action': 'result', 'commandId': 'known'})
        self.assertEqual(reply['structuredContent']['execution']['status'], 'not_dispatched')
        self.assertFalse(reply['structuredContent']['recovery']['executionUncertain'])

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
        self.assertNotEqual(data['suggestedNext'], 'ask_user_choice')

    def test_stale_app_limits_reselection_and_does_not_infer_upgrade_or_restart(self):
        recovery = self.result('STALE_APP')['structuredContent']['recovery']
        self.assertEqual(recovery['status'], 'revalidate_required')
        self.assertEqual(recovery['maxAutomaticChecks'], 2)
        self.assertEqual(recovery['onExhausted'], 'ask_user_choice')
        self.assertFalse(recovery['allowAlternativeTools'])
        self.assertFalse(recovery['replaySideEffects'])
        self.assertIn('不自动关闭或重启应用', recovery['message'])
        self.assertIn('不等于已确认应用升级或退出', recovery['message'])

    def test_busy_has_bounded_checks_then_user_notification(self):
        data = policy('SHARE_NOT_READY', 'not_dispatched')
        self.assertEqual(data['maxAutomaticChecks'], 2)
        self.assertEqual(data['onExhausted'], 'ask_user_choice')

    def test_interruptions_require_user_continuation_without_old_task_replay(self):
        for code in ('TASK_INTERRUPTED', 'TASK_IN_USE'):
            data = policy(code, 'unknown')
            self.assertTrue(data['waitForUser'])
            self.assertFalse(data['replaySideEffects'])
            self.assertTrue(data['executionUncertain'])

    def test_explicit_stop_has_no_continuation_dialog_but_infrastructure_interruption_does(self):
        for code, payload in (('TASK_CANCELLED', None), ('TASK_INTERRUPTED',
                {'interruption': {'explicitStop': True, 'reason': 'host_final_stop_cancelled'}})):
            data = policy(code, 'unknown', payload)
            self.assertTrue(data['taskMustEnd'])
            self.assertFalse(data['waitForUser'])
            self.assertEqual(data['action'], 'respect_explicit_stop')
            self.assertNotIn('userDecision', data)
        data = policy(None, 'not_requested', {'state': 'interrupted',
                      'interruption': {'explicitStop': False, 'reason': 'native_connection_lost'}})
        self.assertFalse(data['taskMustEnd'])
        self.assertEqual(data['userDecision']['presentation'], 'agent_dialog')

    def test_host_stop_evidence_survives_error_wrapper(self):
        interruption = {'explicitStop': True, 'reason': 'host_final_stop_cancelled'}
        with patch.object(server, 'control_snapshot', return_value={'master': True}), \
                patch.object(server, 'reap_task_lease'), patch.object(server, 'resume_idle_task', return_value=None), \
                patch.object(server, 'task_guard', side_effect=server.HelperError(
                    'TASK_INTERRUPTED', 'host stopped', interruption=interruption)):
            result = server.call_tool('quietbridge_task', {'action': 'bind'})
        data = result['structuredContent']
        self.assertEqual(data['interruption'], interruption)
        self.assertEqual(data['suggestedNext'], 'respect_explicit_stop')
        self.assertNotIn('userDecision', data['recovery'])

    def test_target_mismatch_does_not_assume_stale_binding(self):
        data = policy('TARGET_MISMATCH', 'not_dispatched')
        self.assertEqual(data['action'], 'inspect_task_and_original_target')
        self.assertIn('不代表旧任务已失效', data['message'])

    def test_target_conflict_metadata_survives_error_and_does_not_authorize_takeover(self):
        conflict = {'taskId': 'collector-task', 'windowId': 274431, 'hostSessionId': 'collector-session'}
        with patch.object(server, 'capability_block', return_value=None), \
                patch.object(server, 'reap_task_lease'), patch.object(server, 'resume_idle_task', return_value=None), \
                patch.object(server, 'task_guard', side_effect=server.HelperError('TARGET_IN_USE', 'busy', target_conflict=conflict)):
            result = server.call_tool('quietbridge_task', {'action': 'bind'})
        data = result['structuredContent']
        self.assertEqual(data['errorCode'], 'TARGET_IN_USE')
        self.assertEqual(data['targetConflict'], conflict)
        self.assertTrue(data['recovery']['waitForUser'])
        self.assertFalse(data['recovery']['allowAlternativeTools'])
        self.assertIn('停止此会话控制', data['recovery']['message'])

    def test_browser_failures_do_not_recommend_an_immediate_managed_profile_fallback(self):
        for code in ('BROWSER_DEBUGGER_RESTRICTED', 'TASK_SCOPE_REQUIRED'):
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

    def test_confirmed_task_window_closure_recovers_without_inferring_cancellation(self):
        data = policy(None, 'not_requested', {'state': 'invalidated',
                      'targetRecovery': {'status': 'recreate_allowed'}})
        self.assertEqual(data['action'], 'recreate_closed_task_window')
        self.assertFalse(data['waitForUser'])
        self.assertFalse(data['taskMustEnd'])
        self.assertEqual(data['maxAutomaticRecoveries'], 2)
        self.assertIn('recoveryOf=旧taskId', data['message'])

    def test_unproven_loss_only_inspects_and_never_guesses_another_window(self):
        for code in ('TARGET_LOST', 'WINDOW_UNAVAILABLE', 'TASK_INVALIDATED'):
            data = policy(code, 'not_dispatched')
            self.assertEqual(data['action'], 'inspect_lost_target')
            self.assertFalse(data['waitForUser'])
            self.assertIn('不允许选择另一个现存窗口', data['message'])

    def test_repeated_closure_asks_user_in_agent_dialog_and_preserves_business(self):
        data = policy(None, 'not_requested', {'state': 'invalidated',
                      'targetRecovery': {'status': 'decision_required'}})
        self.assertTrue(data['waitForUser'])
        self.assertFalse(data['taskMustEnd'])
        decision = data['userDecision']
        self.assertEqual(decision['presentation'], 'agent_dialog')
        self.assertFalse(decision['autoSelect'])
        self.assertEqual([o['id'] for o in decision['options']], ['continue', 'pause', 'end'])
        self.assertIn('保留进度', data['message'])

    def test_all_decisions_and_exhausted_recovery_share_the_dialog_contract(self):
        for code in ('PERMISSION_DENIED', 'TARGET_IN_USE', 'BROWSER_DEBUGGER_RESTRICTED',
                     'SHARE_NOT_READY', 'STALE_APP', 'SOME_NEW_BLOCKER'):
            data = policy(code, 'not_dispatched')
            self.assertIn('userDecision', data, code)
            self.assertFalse(data['taskMustEnd'])
            self.assertFalse(data['userDecision']['autoSelect'])
        self.assertTrue(policy('TASK_CANCELLED', 'unknown')['taskMustEnd'])
        self.assertNotIn('userDecision', policy('TASK_CANCELLED', 'unknown'))

    def test_background_timeout_observes_before_asking_and_never_replays(self):
        data = policy('BACKGROUND_COMMAND_TIMEOUT', 'unknown')
        self.assertFalse(data['waitForUser'])
        self.assertTrue(data['executionUncertain'])
        self.assertFalse(data['replaySideEffects'])
        self.assertEqual(data['onExhausted'], 'ask_user_choice')

    def test_unresolved_outcome_is_bounded_without_interrupting_normal_queued_polling(self):
        unknown = policy(None, 'unknown')
        self.assertEqual(unknown['maxAutomaticChecks'], 2)
        self.assertEqual(unknown['onExhausted'], 'ask_user_choice')
        queued = policy(None, 'queued')
        self.assertNotIn('userDecision', queued)
        self.assertNotIn('maxAutomaticChecks', queued)
