"""Idle recovery uses fake clocks/helpers and never opens a real window."""
import copy
import json
import threading
import time
import unittest
from unittest import mock

import browser_events as events
import server
import test_task_windows as fixtures


class IdleResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TaskWindowTests('test_bind_truthful_minimal_persistent_identity')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        for patcher in (mock.patch.object(server, 'application_session', return_value='app-session'),
                        mock.patch.object(events, 'application_session', return_value='app-session'),
                        mock.patch.object(server, 'REQUEST_CANCELLED', threading.Event())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.replacements = 0
        original = self.fixture.request

        def request(method, params=None, **kwargs):
            if method == 'getApp':
                self.replacements += 1
                self.assertEqual(params, {'selector': '88'})
                return {'appHandle': 'reconnected', 'pid': 88, 'name': 'Chrome', 'bundleId': 'com.google.Chrome'}
            return original(method, params, **kwargs)

        self.fixture.fake.request.side_effect = request

    def suspend(self, mode='desktop', source='window'):
        task = self.fixture.payload(action='begin', interactionMode=mode)
        bound = self.fixture.payload(action='bind', taskId=task['taskId'], appHandle='a', windowId=7, confirm=True)
        if mode == 'desktop':
            self.fixture.rpc('quietbridge_windows', action='share', appHandle='a', windowId=7, source=source)
        else:
            server.SHARE_SELECTION = {'appHandle': 'a', 'windowId': 7, 'connection': server.current_connection(), 'source': source}
        with server.task_store() as data:
            data['tasks'][bound['taskId']]['lease']['expiresAtUptime'] = time.monotonic() - 1
        server.SESSION_PROTECTION.activity_uptime = time.monotonic() - 121
        server.reap_task_lease()
        self.fixture.fake.request.reset_mock()
        return bound

    def test_wait_then_new_call_recovers_exact_window_and_does_not_replay_click(self):
        task = self.suspend(source='application')
        result = self.fixture.rpc('quietbridge_click', appHandle='a', windowId=7, x=1, y=2)
        data = result['structuredContent']
        self.assertEqual(data['errorCode'], 'TASK_RESUMED')
        self.assertFalse(data['recovery']['waitForUser'])
        self.assertEqual(data['execution']['status'], 'not_dispatched')
        self.assertEqual(data['resumption']['taskId'], task['taskId'])
        self.assertEqual(data['resumption']['appHandle'], 'reconnected')
        previews = [c.args[1] for c in self.fixture.fake.request.call_args_list if c.args[0] == 'preview']
        self.assertEqual(previews[-1]['source'], 'application')
        self.assertEqual(previews[-1]['windowId'], 7)
        self.assertFalse(any(c.args[0] in server.INPUT_METHODS for c in self.fixture.fake.request.call_args_list))
        # A second stale coordinate click is still blocked until observation.
        second = self.fixture.rpc('quietbridge_click', appHandle='reconnected', windowId=7, x=1, y=2)
        self.assertEqual(second['structuredContent']['errorCode'], 'TASK_RESUMED')
        observed = self.fixture.rpc('quietbridge_state', appHandle='reconnected', windowId=7)
        self.assertFalse(observed.get('isError'), observed)
        self.assertNotIn('requiresObservation', server.TASK_BINDINGS[task['taskId']])
        self.assertEqual(self.replacements, 1)

    def test_status_and_heartbeats_describe_recovery_without_reopening_stream(self):
        self.suspend()
        result = self.fixture.rpc(action='status')
        status = json.loads(self.fixture.text(result))
        self.assertEqual(status['state'], 'suspended')
        self.assertTrue(status['resumption']['automatic'])
        self.assertEqual(result['structuredContent']['suggestedNext'], 'restore_original_share')
        self.assertFalse(result['structuredContent']['recovery']['waitForUser'])
        server.handle({'id': 1, 'method': 'ping'})
        server.reap_task_lease()
        self.fixture.fake.request.assert_not_called()

    def test_cancellation_end_and_unshare_discard_recovery_intent(self):
        for stop in ('cancel', 'end', 'unshare'):
            with self.subTest(stop=stop):
                task = self.suspend()
                if stop == 'cancel':
                    server.interrupt_task('client_cancelled')
                elif stop == 'end':
                    self.fixture.payload(action='end', taskId=task['taskId'], confirm=True)
                else:
                    self.fixture.rpc('quietbridge_windows', action='unshare')
                self.assertIsNone(server.IDLE_RESUME)
                self.fixture.fake.request.reset_mock()
                self.fixture.rpc('quietbridge_click', windowId=7, x=1, y=2)
                self.assertFalse(any(c.args[0] == 'getApp' for c in self.fixture.fake.request.call_args_list))

    def test_host_stop_while_waiting_blocks_restore_before_native_io(self):
        self.suspend()
        with mock.patch.object(server, 'host_stop_reason', return_value='host_final_stop_cancelled'):
            result = self.fixture.rpc('quietbridge_state', windowId=7)
        self.assertEqual(result['structuredContent']['errorCode'], 'TASK_INTERRUPTED')
        self.fixture.fake.request.assert_not_called()

    def test_changed_process_generation_never_shares_recycled_window(self):
        task = self.suspend()
        self.fixture.identity['startedSeconds'] = '999'
        result = self.fixture.rpc('quietbridge_state', windowId=7)
        self.assertEqual(result['structuredContent']['errorCode'], 'TASK_INVALIDATED')
        self.assertIsNone(server.IDLE_RESUME)
        self.assertEqual(self.fixture.payload(action='status')['state'], 'invalidated')
        self.assertFalse(any(c.args[0] == 'preview' for c in self.fixture.fake.request.call_args_list))

    def test_wrong_target_does_not_consume_or_transfer_resumption(self):
        self.suspend()
        result = self.fixture.rpc('quietbridge_state', windowId=8)
        self.assertEqual(result['structuredContent']['errorCode'], 'TARGET_MISMATCH')
        self.assertIsNotNone(server.IDLE_RESUME)
        self.fixture.fake.request.assert_not_called()

    def test_expired_native_socket_is_replaced_without_replaying_an_action(self):
        self.suspend()
        server.IDLE_RESUME['nativeRetained'] = True
        original = self.fixture.fake.request.side_effect

        def request(method, params=None, **kwargs):
            if method == 'preview' and params == {'action': 'renew'}:
                raise server.HelperError('TASK_INTERRUPTED', 'native idle lease revoked')
            return original(method, params, **kwargs)

        self.fixture.fake.request.side_effect = request
        self.fixture.fake._stop.reset_mock()
        result = self.fixture.rpc('quietbridge_click', windowId=7, x=1, y=2)
        self.assertEqual(result['structuredContent']['errorCode'], 'TASK_RESUMED')
        self.fixture.fake._stop.assert_called_once()
        self.assertEqual(self.replacements, 1)
        self.assertFalse(any(c.args[0] in server.INPUT_METHODS for c in self.fixture.fake.request.call_args_list))

    def test_target_claimed_by_another_session_is_not_reacquired(self):
        task = self.suspend()
        with mock.patch('session_registry.claim_target', side_effect=ValueError('TARGET_IN_USE: another session')):
            result = self.fixture.rpc('quietbridge_state', windowId=7)
        self.assertEqual(result['structuredContent']['errorCode'], 'TARGET_IN_USE')
        self.assertEqual(self.fixture.payload(action='status')['state'], 'suspended')
        self.assertNotIn(task['taskId'], server.TASK_BINDINGS)
        self.assertFalse(any(c.args[0] == 'preview' for c in self.fixture.fake.request.call_args_list))

    def test_new_application_session_requires_explicit_revalidation(self):
        self.suspend()
        with mock.patch.object(server, 'application_session', return_value='replacement-app'):
            result = self.fixture.rpc('quietbridge_state', windowId=7)
        self.assertEqual(result['structuredContent']['errorCode'], 'REQUIRES_REVALIDATION')
        self.fixture.fake.request.assert_not_called()

    def test_read_only_and_disabled_capabilities_do_not_restore_resources(self):
        self.suspend()
        with mock.patch.object(server, 'READ_ONLY', True):
            result = self.fixture.rpc('quietbridge_state', windowId=7)
        self.assertNotEqual(result['structuredContent']['errorCode'], 'TASK_RESUMED')
        with mock.patch.object(server, 'control_snapshot', return_value=None):
            result = self.fixture.rpc('quietbridge_state', windowId=7)
        self.assertEqual(result['structuredContent']['errorCode'], 'APP_NOT_RUNNING')
        self.fixture.fake.request.assert_not_called()
        self.assertIsNotNone(server.IDLE_RESUME)

    def test_permission_failure_keeps_task_suspended_for_later_new_activity(self):
        task = self.suspend()
        with mock.patch.object(server, 'ensure_share', side_effect=server.HelperError('SHARE_NOT_READY', 'locked')):
            result = self.fixture.rpc('quietbridge_state', windowId=7)
        self.assertEqual(result['structuredContent']['errorCode'], 'SHARE_NOT_READY')
        self.assertEqual(self.fixture.payload(action='status')['state'], 'suspended')
        self.assertNotIn(task['taskId'], server.TASK_BINDINGS)
        self.assertIsNotNone(server.IDLE_RESUME)

    def test_browser_resume_rotates_document_binding_and_requires_new_mapping(self):
        task = self.suspend(mode='background_browser')
        task_id = task['taskId']
        previous = {'taskId': task_id, 'bindingId': 'c' * 32, 'token': 'd' * 64,
                    'assignmentVersion': 1, 'tabId': 9, 'rootTabId': 7, 'browserWindowId': 2,
                    'ownedTabs': [{'tabId': 7, 'browserWindowId': 2}, {'tabId': 9, 'browserWindowId': 2}],
                    'origin': 'chrome-extension://' + 'a' * 32 + '/', 'paused': False,
                    'globalOrigins': True, 'selector': '', 'authorizedOrigin': 'https://example.org',
                    'event': {'documentId': 'stale'}, 'mappingVerified': True, 'mapping': {'windowId': 7}}
        server.IDLE_RESUME['browser'] = copy.deepcopy(previous)
        path = self.fixture.path.with_name('browser-events.json')
        with events.event_store(path) as data:
            data['tasks'] = {}
            data['peer'] = {'enabled': True, 'permission': True, 'origin': previous['origin'],
                            'applicationSession': 'app-session', 'lastSeen': time.time(),
                            'assignmentTitle': 'old-title', 'snapshot': {'snapshotId': 'old-ref'},
                            'command': {'requestId': 'e' * 32, 'taskId': task_id, 'operation': 'click', 'delivered': True}}
        result = self.fixture.rpc('quietbridge_browser_events', action='command', taskId=task_id,
                                  operation='click', ref=1, snapshotId='old-ref')
        self.assertEqual(result['structuredContent']['errorCode'], 'TASK_RESUMED')
        record = events.read_file(path)['tasks'][task_id]
        self.assertEqual(record['tabId'], 9)
        self.assertEqual(record['rootTabId'], 7)
        self.assertEqual(record['ownedTabs'], previous['ownedTabs'])
        self.assertNotEqual(record['bindingId'], previous['bindingId'])
        self.assertIsNone(record['event'])
        self.assertFalse(record['mappingVerified'])
        peer = events.read_file(path)['peer']
        self.assertIsNone(peer['command'])
        self.assertIsNone(peer['snapshot'])
        self.assertTrue(peer['commandCanceled']['mayHaveExecuted'])
        self.assertNotIn('assignmentTitle', peer)
        steps = result['structuredContent']['resumption']['nextSteps']
        self.assertEqual(steps[0]['arguments']['action'], 'verify_mapping')
        self.assertEqual(steps[1]['arguments']['source'], 'window')
        self.assertEqual(steps[-1]['arguments']['operation'], 'snapshot')
        self.assertFalse(any(c.args[0] in server.INPUT_METHODS for c in self.fixture.fake.request.call_args_list))

    def test_private_browser_channel_can_restart_from_fresh_root_consent(self):
        task = self.suspend(mode='background_browser')
        previous = {'taskId': task['taskId'], 'origin': 'chrome-extension://' + 'a' * 32 + '/',
                    'tabId': 9, 'browserWindowId': 2, 'assignmentVersion': 8,
                    'bindingId': 'b' * 32, 'token': 'c' * 64, 'paused': False}
        private = self.fixture.path.with_name('browser-events.json')
        root = self.fixture.path.parent / 'root-browser-events.json'
        stale = {'enabled': True, 'permission': True, 'origin': previous['origin'],
                 'applicationSession': 'app-session', 'lastSeen': time.time() - 180,
                 'clientVersion': '0.4.10'}
        with events.event_store(private) as data:
            data['peer'] = stale
        with events.event_store(root) as data:
            data['peer'] = {**stale, 'lastSeen': time.time()}
        task = copy.deepcopy(server.IDLE_RESUME['record'])
        task['state'] = 'bound'
        events.resume_assignment(previous, task, self.fixture.path, root)
        current = events.read_file(private)['tasks'][task['taskId']]
        self.assertGreater(current['assignmentVersion'], previous['assignmentVersion'])
        self.assertEqual(current['tabId'], 9)
        self.assertIsNone(current['event'])

    def test_explicit_browser_pause_is_never_undone_by_idle_recovery(self):
        task = self.suspend(mode='background_browser')
        previous = {'taskId': task['taskId'], 'origin': 'chrome-extension://' + 'a' * 32 + '/',
                    'tabId': 9, 'browserWindowId': 2, 'assignmentVersion': 1, 'paused': False}
        private = self.fixture.path.with_name('browser-events.json')
        with events.event_store(private) as data:
            data['peer'] = {'enabled': True, 'permission': True, 'origin': previous['origin'],
                            'applicationSession': 'app-session', 'lastSeen': time.time()}
            data['tasks'] = {task['taskId']: {**previous, 'paused': True}}
        with self.assertRaisesRegex(ValueError, 'STALE_TARGET'):
            events.resume_assignment(previous, server.IDLE_RESUME['record'], self.fixture.path)
        self.assertTrue(events.read_file(private)['tasks'][task['taskId']]['paused'])


if __name__ == '__main__':
    unittest.main()
