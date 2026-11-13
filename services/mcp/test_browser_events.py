import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# SourceFileLoader 在模块代码执行前就可能写缓存，必须在首次 load 前禁用。
# 扩展目录中的 __pycache__ 会让 Chrome 拒绝加载整个扩展。
sys.dont_write_bytecode = True

# 运行态默认目录是真实用户的 ~/Library/Application Support；测试必须隔离，
# 否则会读写本机的任务记录。
os.environ['QUIETBRIDGE_CONFIG_DIR'] = tempfile.mkdtemp(prefix='quietbridge-test-config-')

import browser_events as events
import server

EXT = Path(__file__).resolve().parents[2] / 'extensions/chromium'


def load(name):
    spec = importlib.util.spec_from_file_location(name, EXT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load('native_host')
installer = load('install_native_host')
ORIGIN = 'chrome-extension://' + 'a' * 32 + '/'
DERIVED_ID = installer.extension_id_from_manifest()
DERIVED_ORIGIN = 'chrome-extension://' + DERIVED_ID + '/'


class BrowserEventsTests(unittest.TestCase):
    def test_observer_failure_is_connected_but_not_ready_and_does_not_queue_commands(self):
        token = self.assign()
        event = self.event()
        self.request({'action': 'event', 'taskId': 'task', 'token': token, 'event': event})
        observation = {'state': 'failed', 'bindingId': event['bindingId'], 'documentId': 'doc',
            'navigationEpoch': 1, 'attempt': 2, 'stage': 'observer_configure',
            'code': 'BROWSER_LIFECYCLE_TIMEOUT', 'at': events.time.time()}
        issue = {k: observation[k] for k in ('stage', 'code', 'at')}
        self.sync(observerStatus=observation, lifecycleError=issue)
        status = events.status('task', **self.paths)
        self.assertEqual(status['nativeConnection'], 'connected')
        self.assertEqual(status['executionPolicy']['waitingReason'], 'observer_not_ready')
        self.assertEqual(status['observerStatus'], observation)
        self.assertEqual(status['lifecycleError'], issue)
        with self.assertRaisesRegex(ValueError, 'BROWSER_OBSERVER_NOT_READY'):
            events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'}, **self.paths)
        self.assertFalse(events.read_file(self.store)['peer'].get('command'))
        self.sync(observerStatus={**observation, 'state': 'ready', 'code': None})
        self.assertIsNone(events.status('task', **self.paths)['executionPolicy']['waitingReason'])
        result = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'}, **self.paths)
        self.assertTrue(result['commandId'])

    def test_late_observer_status_does_not_describe_a_new_document(self):
        token = self.assign()
        event = self.event()
        self.request({'action': 'event', 'taskId': 'task', 'token': token, 'event': event})
        observation = {'state': 'failed', 'bindingId': event['bindingId'], 'documentId': 'old-doc',
            'navigationEpoch': 1, 'attempt': 1, 'stage': 'observer_load',
            'code': 'BROWSER_LIFECYCLE_TIMEOUT', 'at': events.time.time()}
        self.sync(observerStatus=observation)
        status = events.status('task', **self.paths)
        self.assertIsNone(status['observerStatus'])
        self.assertIsNone(status['executionPolicy']['waitingReason'])
        self.assertEqual(status['nativeConnection'], 'connected')

    def setUp(self):
        waiting = mock.patch.object(events, 'CANDIDATE_QUERY_TIMEOUT', 0)
        waiting.start()
        self.addCleanup(waiting.stop)
        session = mock.patch.object(events, 'application_session', return_value='test-app-session')
        session.start()
        self.addCleanup(session.stop)
        controls = mock.patch.object(server, 'control_snapshot', return_value={'master': True, 'browser': True})
        controls.start()
        self.addCleanup(controls.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.tasks = self.base / 'task-windows.json'
        self.store = self.base / 'browser-events.json'
        self.tasks.write_text(json.dumps({'version': 1, 'activeTaskId': 'task', 'tasks': {
            'task': {'taskId': 'task', 'state': 'bound', 'windowId': 900, 'pid': 88,
                     'processIdentity': {'pid': 88, 'startedSeconds': '10', 'startedMicroseconds': '1'},
                     'sessionId': 'session'},
            'other': {'taskId': 'other', 'state': 'bound'}}}))
        self.paths = {'task_file': self.tasks, 'event_file': self.store}

    def await_window(self):
        data = events.read_file(self.tasks)
        data['tasks']['task']['state'] = 'awaiting_window'
        for key in ('windowId', 'pid', 'processIdentity', 'sessionId'):
            data['tasks']['task'].pop(key, None)
        self.tasks.write_text(json.dumps(data))

    def bind_native_window(self):
        data = events.read_file(self.tasks)
        data['tasks']['task'].update(state='bound', windowId=900, pid=88,
            processIdentity={'pid': 88, 'startedSeconds': '10', 'startedMicroseconds': '1'}, sessionId='session')
        self.tasks.write_text(json.dumps(data))

    def request(self, message, origin=ORIGIN):
        return events.dispatch(message, origin, [ORIGIN], **self.paths)

    def assign(self, task='task', tab=7):
        self.sync()
        events.internal_action({'action': 'candidates'}, **self.paths)
        self.sync(candidates=[{'tabId': tab, 'browserWindowId': 2, 'url': 'https://example.org/path'}])
        events.internal_action({'action': 'assign', 'taskId': task, 'tabId': tab,
                               'browserWindowId': 2}, **self.paths)
        return self.sync()['assignment']['token']

    def event(self, sequence=1):
        binding_id = events.read_file(self.store, {'tasks': {}})['tasks'].get('task', {}).get('bindingId', 'a' * 32)
        return {'browserWindowId': 2, 'tabId': 7, 'documentId': 'doc',
                'navigationEpoch': 1, 'url': 'https://example.org/t/123', 'phase': 'content',
                'loaded': False, 'domReady': True, 'contentReady': None, 'bodyPresent': True,
                'bodyVisible': True, 'elementCount': 3, 'readyState': 'interactive',
                'conditionMatched': None, 'bindingId': binding_id,
                'sequence': sequence, 'mappingVerified': False}

    def test_legacy_preview_lease_and_pixels_cannot_restart_automatic_capture(self):
        from browser_preview import identity
        token = self.assign()
        self.request({'action': 'event', 'taskId': 'task', 'token': token, 'event': self.event()})
        with events.event_store(self.store) as data:
            current = identity(data['tasks']['task'])
            lease = {**current, 'leaseId': 'c' * 32, 'expiresAt': events.time.time() + 3}
            data['peer']['previewLease'] = lease
            data['peer']['pendingVerify'] = {'operation': 'click'}
        frame = {'leaseId': lease['leaseId'], 'identity': current,
                 'data': 'ZmFrZQ==', 'capturedAt': events.time.time()}
        response = self.sync(previewFrame=frame)
        self.assertIsNone(response['previewLease'])
        peer = events.read_file(self.store)['peer']
        self.assertNotIn('previewFrame', peer)
        self.assertNotIn('previewLease', peer)
        self.assertEqual(peer['pendingVerify'], {'operation': 'click'})
        self.assertFalse(peer.get('commandResults'))
        self.assertNotIn('ZmFrZQ==', json.dumps(events.status('task', **self.paths)))
        response = self.sync(previewFrame=frame)
        self.assertIsNone(response['previewLease'])
        self.assertNotIn('previewFrame', events.read_file(self.store)['peer'])

    def test_assign_event_status_unbind_full_chain(self):
        original = self.tasks.read_bytes()
        token = self.assign()
        result = self.request({'action': 'event', 'taskId': 'task', 'token': token, 'event': self.event()})
        self.assertTrue(result['ok'])
        result = self.request({'action': 'status', 'taskId': 'task'})['status']
        self.assertFalse(result['mappingVerified'])
        self.assertNotIn('token', result)
        self.assertIsNone(result['event']['contentReady'])
        self.assertIsNone(result['event']['conditionMatched'])
        self.assertFalse(result['event']['loaded'])
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.tasks.read_bytes(), original)
        self.request({'action': 'unbind', 'taskId': 'task', 'token': token})
        self.assertFalse(events.status('task', **self.paths)['paired'])

    def test_application_exit_rejects_browser_control_and_old_session_cannot_resume(self):
        token = self.assign()
        event = self.event()
        before = self.store.read_bytes()
        with mock.patch.object(events, 'application_session', return_value=None):
            self.assertEqual(self.request({'action': 'ping'})['code'], 'APP_NOT_RUNNING')
            self.assertEqual(self.request({'action': 'sync', 'enabled': True, 'permission': True})['code'], 'APP_NOT_RUNNING')
            with self.assertRaises(ValueError):
                events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'}, **self.paths)
        self.assertEqual(self.store.read_bytes(), before)
        with mock.patch.object(events, 'application_session', return_value='new-app-session'):
            self.assertFalse(events.status('task', **self.paths)['paired'])
            with self.assertRaisesRegex(ValueError, 'APP_SESSION_EXPIRED'):
                self.request({'action': 'event', 'taskId': 'task', 'token': token, 'event': event})
            reply = self.sync(applicationSession='test-app-session',
                              created={'requestId': 'f' * 32, 'tabId': 7, 'browserWindowId': 2},
                              commandResult={'requestId': 'f' * 32, 'status': 'error', 'error': 'late result'})
            self.assertTrue(reply['sessionReset'])
            self.assertIsNone(reply.get('assignment'))
            data = events.read_file(self.store)
            self.assertEqual(data['peer']['applicationSession'], 'new-app-session')
            self.assertEqual(data['tasks'], {})

    def test_create_target_works_before_binding(self):
        # 建窗必须发生在绑定之前，因此不能用 sessionId 作为前置条件。
        self.await_window()
        self.sync()
        queued = events.internal_action({'action': 'create_target', 'taskId': 'task',
                                         'url': 'https://example.org/'}, **self.paths)
        self.assertTrue(queued['requested'])
        self.assertEqual(self.sync()['createRequest']['url'], 'https://example.org/')

    def test_create_target_is_single_flight_and_bound_to_active_task(self):
        self.await_window()
        self.sync()
        first = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        self.assertTrue(first['requested'])
        self.assertRegex(first['requestId'], '^[0-9a-f]{32}$')
        self.assertEqual(self.sync()['createRequest']['requestId'], first['requestId'])
        repeated = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        self.assertTrue(repeated['reused'])
        self.assertEqual(repeated['requestId'], first['requestId'])
        with self.assertRaises(ValueError):
            events.internal_action({'action': 'create_target', 'taskId': 'other'}, **self.paths)

    def test_created_result_needs_the_pending_request_and_clears_it(self):
        self.await_window()
        self.sync()
        request = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        created = {'requestId': request['requestId'], 'browserWindowId': 5, 'tabId': 9}
        with self.assertRaises(ValueError):
            self.sync(created=created | {'requestId': '0' * 32})
        self.assertTrue(self.sync(created=created)['ok'])
        self.assertIsNone(self.sync()['createRequest'])
        self.assertTrue(self.sync(created=created)['ok'], '确认响应丢失后允许相同证据再次回传')
        with self.assertRaises(ValueError):
            self.sync(created=created | {'tabId': 10})

    def test_assign_distinguishes_created_and_selected_windows(self):
        self.await_window()
        self.sync()
        request = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        self.sync(created={'requestId': request['requestId'], 'browserWindowId': 2, 'tabId': 7})
        self.bind_native_window()
        events.internal_action({'action': 'candidates'}, **self.paths)
        self.sync(candidates=[{'tabId': 7, 'browserWindowId': 2, 'url': 'https://example.org/path'}])
        result = events.internal_action({'action': 'assign', 'taskId': 'task', 'tabId': 7,
                                         'browserWindowId': 2}, **self.paths)
        self.assertEqual(result['targetEvidence']['mode'], 'created')
        self.assertEqual(result['targetEvidence']['createdRequestId'], request['requestId'])
        self.assertEqual(self.sync()['assignment']['targetEvidence']['mode'], 'created')

    def test_created_navigation_outcome_is_visible_and_does_not_authorize_blank_assignment(self):
        for navigation in ('issued', 'failed', 'unknown', 'not_requested'):
            with self.subTest(navigation=navigation):
                self.await_window()
                self.sync(enabled=False)
                self.sync()
                request = events.internal_action({'action': 'create_target', 'taskId': 'task',
                                                  'url': 'https://example.org/'}, **self.paths)
                created = {'requestId': request['requestId'], 'browserWindowId': 2, 'tabId': 7,
                           'navigationState': navigation}
                if navigation in ('failed', 'unknown'):
                    created['navigationError'] = 'test navigation error'
                self.sync(created=created)
                result = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
                self.assertEqual(result['navigationState'], navigation)
                self.assertFalse(result['requested'])
                self.assertEqual(result.get('navigationError'), created.get('navigationError'))
                self.assertEqual(result['nextAction'], 'candidates_then_assign' if navigation == 'issued' else 'inspect_created_tab')
                self.bind_native_window()
                status = events.status('task', **self.paths)
                self.assertEqual(status['createdTarget']['navigationState'], navigation)
                self.assertEqual(status['createdTarget'].get('navigationError'), created.get('navigationError'))
                events.internal_action({'action': 'candidates'}, **self.paths)
                self.sync(candidates=[])
                with self.assertRaisesRegex(ValueError, '缺少扩展目标证据'):
                    events.internal_action({'action': 'assign', 'taskId': 'task', 'tabId': 7,
                                            'browserWindowId': 2}, **self.paths)

    def test_created_navigation_report_rejects_invalid_fields(self):
        self.await_window()
        self.sync()
        request = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        created = {'requestId': request['requestId'], 'browserWindowId': 2, 'tabId': 7}
        for extra in ({'navigationState': 'loaded'}, {'navigationError': 'missing state'},
                      {'navigationState': 'issued', 'navigationError': 'invalid success'},
                      {'navigationState': 'failed', 'navigationError': 'x' * 201}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.sync(created=created | extra)

    def test_selected_window_is_not_reported_as_created(self):
        self.prepare_assignment()
        self.assertEqual(self.sync()['assignment']['targetEvidence']['mode'], 'selected')
        self.assertIsNone(self.sync()['assignment']['targetEvidence']['createdRequestId'])

    def test_created_window_belongs_only_to_its_active_task(self):
        # 单活动任务模型：切换活动任务后，旧建窗证据失效，新任务可按普通候选选择。
        self.await_window()
        self.sync()
        request = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        self.sync(created={'requestId': request['requestId'], 'browserWindowId': 2, 'tabId': 7})
        data = events.read_file(self.tasks)
        data['activeTaskId'] = 'other'
        data['tasks']['other'].update(windowId=901, pid=88,
            processIdentity={'pid': 88, 'startedSeconds': '10', 'startedMicroseconds': '1'}, sessionId='session')
        self.tasks.write_text(json.dumps(data))
        events.internal_action({'action': 'candidates'}, **self.paths)
        self.sync(candidates=[{'tabId': 7, 'browserWindowId': 2, 'url': 'https://example.org/path'}])
        result = events.internal_action({'action': 'assign', 'taskId': 'other', 'tabId': 7,
                                         'browserWindowId': 2}, **self.paths)
        self.assertEqual(result['targetEvidence']['mode'], 'selected')
        self.assertIsNone(result['targetEvidence']['createdRequestId'])

    def test_pending_creation_is_reused_even_after_thirty_seconds(self):
        self.await_window()
        self.sync()
        first = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        with events.event_store(self.store) as data:
            data['peer']['createRequest']['issuedAt'] -= 60
        repeated = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        self.assertEqual(repeated['requestId'], first['requestId'])
        self.assertTrue(repeated['reused'])
        with self.assertRaisesRegex(ValueError, '已有创建请求'):
            events.internal_action({'action': 'create_target', 'taskId': 'task',
                                    'url': 'https://example.org/new'}, **self.paths)

    def test_created_target_is_reused_and_bound_task_cannot_create(self):
        self.await_window()
        self.sync()
        first = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        self.sync(created={'requestId': first['requestId'], 'browserWindowId': 2, 'tabId': 7})
        repeated = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        self.assertFalse(repeated['requested'])
        self.assertTrue(repeated['reused'])
        self.assertEqual((repeated['browserWindowId'], repeated['tabId']), (2, 7))
        self.assertIsNone(self.sync()['createRequest'])
        self.bind_native_window()
        with self.assertRaisesRegex(ValueError, '已有目标须复用'):
            events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)

    def test_candidates_accepts_current_task_id_without_losing_permissions(self):
        self.sync()
        result = events.internal_action({'action': 'candidates', 'taskId': 'task'}, **self.paths)
        self.assertEqual(result['nativeConnection'], 'connected')
        with self.assertRaisesRegex(ValueError, '当前活动任务'):
            events.internal_action({'action': 'candidates', 'taskId': 'other'}, **self.paths)
        self.request({'action': 'sync', 'enabled': True, 'permission': False})
        result = events.internal_action({'action': 'candidates', 'taskId': 'task'}, **self.paths)
        self.assertFalse(result['globalPermission'])
        self.assertEqual(result['candidates'], [])

    def test_candidates_waits_for_first_metadata_without_second_agent_call(self):
        self.sync()
        candidate = {'tabId': 7, 'browserWindowId': 2, 'url': 'https://example.org/path'}
        def reply(_):
            self.sync(candidates=[candidate])
        with mock.patch.object(events, 'CANDIDATE_QUERY_TIMEOUT', 3), \
                mock.patch.object(events.time, 'sleep', side_effect=reply) as wait:
            result = events.internal_action({'action': 'candidates', 'taskId': 'task'}, **self.paths)
        self.assertEqual(result['candidates'], [candidate])
        self.assertFalse(result['refreshPending'])
        self.assertEqual(result['refreshStatus'], 'ready')
        wait.assert_called_once()

    def test_candidates_distinguishes_pending_from_fresh_empty_and_can_cancel(self):
        self.sync()
        pending = events.internal_action({'action': 'candidates'}, **self.paths)
        self.assertTrue(pending['refreshPending'])
        self.assertEqual(pending['retryAfterMs'], 1000)
        with mock.patch.object(events, 'CANDIDATE_QUERY_TIMEOUT', 3), \
                mock.patch.object(events.time, 'sleep', side_effect=lambda _: self.sync(candidates=[])):
            empty = events.internal_action({'action': 'candidates'}, **self.paths)
        self.assertEqual(empty['candidates'], [])
        self.assertFalse(empty['refreshPending'])
        checks = mock.Mock(side_effect=[None, RuntimeError('canceled')])
        with self.assertRaisesRegex(RuntimeError, 'canceled'):
            events.internal_action({'action': 'candidates'}, **self.paths, check_cancelled=checks)

    def test_candidates_wait_stops_when_current_task_changes(self):
        self.sync()
        def changed(_):
            data = events.read_file(self.tasks)
            data['activeTaskId'] = 'other'
            self.tasks.write_text(json.dumps(data))
        with mock.patch.object(events, 'CANDIDATE_QUERY_TIMEOUT', 3), \
                mock.patch.object(events.time, 'sleep', side_effect=changed):
            with self.assertRaisesRegex(ValueError, '当前活动任务'):
                events.internal_action({'action': 'candidates', 'taskId': 'task'}, **self.paths)

    def test_candidates_with_fresh_cache_never_waits_or_loses_results(self):
        self.assign()
        with mock.patch.object(events.time, 'sleep') as wait:
            result = events.internal_action({'action': 'candidates'}, **self.paths)
        self.assertEqual(result['candidates'][0]['tabId'], 7)
        wait.assert_not_called()

    def test_bad_fields_report_names_without_values(self):
        with self.assertRaises(ValueError) as caught:
            events.internal_action({'action': 'assign', 'taskId': 'task', 'tabId': 7,
                                    'unexpected': 'content-must-not-be-reported'}, **self.paths)
        self.assertIn('缺少 [browserWindowId]', str(caught.exception))
        self.assertIn('多余 [unexpected]', str(caught.exception))
        self.assertNotIn('content-must-not-be-reported', str(caught.exception))

    SNAPSHOT = {'snapshotId': 'snap-1', 'url': 'https://example.org/path', 'readyState': 'complete', 'elementCount': 12,
                'elements': [{'ref': 1, 'tag': 'a', 'role': 'link', 'name': 'Docs',
                              'disabled': False, 'x': 0, 'y': 0, 'width': 10, 'height': 10}]}

    def test_command_requires_assignment_and_is_single_flight(self):
        self.sync()
        with self.assertRaises(ValueError):
            events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'},
                                   **self.paths)
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'},
                                        **self.paths)
        self.assertEqual(queued['status'], 'queued')
        self.assertEqual(self.sync()['command']['requestId'], queued['commandId'])
        with self.assertRaises(ValueError):
            events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'},
                                   **self.paths)
        with self.assertRaises(ValueError):
            events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'click'},
                                   **self.paths)

    def test_command_result_is_polled_once_and_not_replayed(self):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'},
                                        **self.paths)
        poll = {'action': 'result', 'taskId': 'task', 'commandId': queued['commandId']}
        self.assertEqual(events.internal_action(poll, **self.paths)['status'], 'pending')
        self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': self.SNAPSHOT})
        reply = events.internal_action(poll, **self.paths)
        self.assertEqual(reply['status'], 'ok')
        self.assertEqual(reply['result']['elements'][0]['role'], 'link')
        self.assertIsNone(self.sync()['command'])
        self.assertEqual(events.internal_action(poll | {'commandId': 'f' * 32}, **self.paths)['status'],
                         'unknown')

    def test_command_error_result_stays_an_error(self):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'},
                                        **self.paths)
        self.sync(commandResult={'requestId': queued['commandId'], 'status': 'error',
                                 'error': '缺少已核验的文档身份'})
        reply = events.internal_action({'action': 'result', 'taskId': 'task',
                                        'commandId': queued['commandId']}, **self.paths)
        self.assertEqual(reply['status'], 'error')
        self.assertNotIn('result', reply)

    def test_unsolicited_or_malformed_snapshot_is_rejected(self):
        self.sync()
        with self.assertRaises(ValueError):
            self.sync(commandResult={'requestId': 'a' * 32, 'status': 'error', 'error': 'x'})
        self.prepare_command()
        broken = self.SNAPSHOT | {'elements': [self.SNAPSHOT['elements'][0] | {'ref': 0}]}
        extra = self.SNAPSHOT | {'elements': [self.SNAPSHOT['elements'][0] | {'href': 'x'}]}
        for result in (broken, extra):
            queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'}, **self.paths)
            reply = self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': result})
            self.assertEqual(reply['commandAck']['code'], 'INVALID_BROWSER_RESULT')
            self.assertIsNone(reply['command'])
            self.assertEqual(self.sync()['taskAssignment'], 'assigned', '坏回执不能毒化下次握手')
            stored = events.read_file(self.store)['peer']['commandResults'][-1]
            self.assertEqual(stored['status'], 'error')
            self.assertNotIn('result', stored)

    def test_action_commands_validate_their_own_parameters(self):
        self.prepare_command()
        submit = lambda **extra: events.internal_action(  # noqa: E731
            dict({'action': 'command', 'taskId': 'task', 'snapshotId': 'snap-1'}, **extra), **self.paths)
        with self.assertRaises(ValueError):
            submit(operation='fill', value='a')
        with self.assertRaises(ValueError):
            submit(operation='click', ref=1, value='a')
        with self.assertRaises(ValueError):
            submit(operation='scroll', deltaY=11)
        with self.assertRaises(ValueError):
            submit(operation='click', ref=0)
        with self.assertRaises(ValueError):
            submit(operation='select', ref=1, value='x' * 1001)
        queued = submit(operation='fill', ref=1, value='hello')
        self.assertEqual(queued['status'], 'queued')
        self.assertTrue(self.sync()['command']['params'] == {'ref': 1, 'value': 'hello', 'snapshotId': 'snap-1'})

    def test_action_result_reports_performed_and_verified_separately(self):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                         'operation': 'click', 'snapshotId': 'snap-1', 'ref': 3}, **self.paths)
        self.assertTrue(self.sync()['command']['operation'] == 'click')
        self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok',
                                 'result': {'performed': True, 'verified': False,
                                            'observed': {'tag': 'a'}}})
        reply = events.internal_action({'action': 'result', 'taskId': 'task',
                                        'commandId': queued['commandId']}, **self.paths)
        self.assertEqual(reply['status'], 'ok')
        self.assertEqual(reply['result']['operation'], 'click')
        self.assertTrue(reply['result']['performed'])
        self.assertFalse(reply['result']['verified'])

    def test_scroll_requires_named_direction_and_normalizes_before_extension(self):
        for args, expected in (({'direction': 'down'}, -6), ({'direction': 'up', 'lines': 3}, 3),
                               ({'direction': 'down', 'lines': 10}, -10)):
            with self.subTest(args=args):
                params = events.command_params({'snapshotId': 'snap', **args}, 'scroll')
                self.assertEqual(params, {'snapshotId': 'snap', 'deltaY': expected})
        for args in ({}, {'lines': 6}, {'deltaY': -6}, {'deltaY': 6}, {'direction': 'left'},
                     {'direction': 'down', 'deltaY': -6}, {'direction': 'down', 'lines': 0},
                     {'direction': 'down', 'lines': True}, {'direction': 'down', 'lines': 11}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                events.command_params({'snapshotId': 'snap', **args}, 'scroll')

        schema = events.make_tool(lambda: self.tasks)['inputSchema']
        self.assertNotIn('deltaY', schema['properties'])
        scroll_shapes = [shape for shape in schema['oneOf'] if shape['properties'].get('operation') == {'const': 'scroll'}]
        self.assertEqual(len(scroll_shapes), 1)
        self.assertIn('direction', scroll_shapes[0]['required'])

    def test_scroll_named_direction_reaches_extension_as_downward_delta(self):
        self.prepare_command()
        events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'scroll',
                                'snapshotId': 'snap-1', 'direction': 'down', 'lines': 6}, **self.paths)
        self.assertEqual(self.sync()['command']['params'], {'snapshotId': 'snap-1', 'deltaY': -6})

    def test_action_result_accepts_bounded_float_coordinates(self):
        for extra in ({'x': 123.5, 'y': 240.25}, {'x': 12, 'y': 40}):
            self.prepare_command()
            queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                             'operation': 'scroll', 'snapshotId': 'snap-1',
                                             'direction': 'down', 'lines': 3}, **self.paths)
            result = {'performed': True, 'verified': False,
                      'observed': {'route': 'cdp', 'tabId': 7, **extra}}
            reply = self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': result})
            self.assertEqual(reply['commandAck']['status'], 'accepted', '浮点坐标不得导致回执被拒')
            got = events.internal_action({'action': 'result', 'taskId': 'task',
                                          'commandId': queued['commandId']}, **self.paths)
            self.assertEqual(got['status'], 'ok')
            self.assertEqual(got['result']['observed']['x'], extra['x'])
        for bad in ({'x': float('nan')}, {'x': float('inf')}, {'x': 1e12}):
            self.prepare_command()
            queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                             'operation': 'scroll', 'snapshotId': 'snap-1',
                                             'direction': 'down', 'lines': 3}, **self.paths)
            result = {'performed': True, 'verified': False, 'observed': bad}
            reply = self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': result})
            self.assertEqual(reply['commandAck']['status'], 'rejected', 'NaN/Inf/超界坐标必须拒绝')

    def test_malformed_action_result_is_rejected(self):
        for result in ({'performed': True, 'verified': True, 'observed': {'note': 'x' * 300}},
                       {'performed': 'yes', 'verified': True, 'observed': {}}):
            self.prepare_command()
            queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                             'operation': 'scroll', 'snapshotId': 'snap-1',
                                             'direction': 'down', 'lines': 3}, **self.paths)
            reply = self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': result})
            self.assertEqual(reply['commandAck']['status'], 'rejected')
            self.assertIsNone(self.sync()['command'], '拒绝后的动作不得重放')
            self.assertTrue(events.status('task', **self.paths)['commandState']['verificationRequired'])

    def test_performed_action_requires_a_snapshot_before_the_next_action(self):
        self.prepare_command()
        poll = lambda cid: events.internal_action(  # noqa: E731
            {'action': 'result', 'taskId': 'task', 'commandId': cid}, **self.paths)
        click = events.internal_action({'action': 'command', 'taskId': 'task',
                                        'operation': 'click', 'snapshotId': 'snap-1', 'ref': 3}, **self.paths)
        self.sync(commandResult={'requestId': click['commandId'], 'status': 'ok',
                                 'result': {'performed': True, 'verified': False,
                                            'observed': {'tag': 'a'}}})
        self.assertEqual(poll(click['commandId'])['nextAction'], 'snapshot')
        with self.assertRaises(ValueError):
            events.internal_action({'action': 'command', 'taskId': 'task',
                                    'operation': 'click', 'snapshotId': 'snap-1', 'ref': 3}, **self.paths)
        shot = events.internal_action({'action': 'command', 'taskId': 'task',
                                       'operation': 'snapshot'}, **self.paths)
        self.sync(commandResult={'requestId': shot['commandId'], 'status': 'ok',
                                 'result': self.SNAPSHOT})
        self.assertEqual(poll(shot['commandId'])['nextAction'], 'observe')
        events.internal_action({'action': 'command', 'taskId': 'task',
                                'operation': 'click', 'snapshotId': 'snap-1', 'ref': 3}, **self.paths)

    def test_failed_action_does_not_demand_verification(self):
        self.prepare_command()
        click = events.internal_action({'action': 'command', 'taskId': 'task',
                                        'operation': 'click', 'snapshotId': 'snap-1', 'ref': 99}, **self.paths)
        self.sync(commandResult={'requestId': click['commandId'], 'status': 'ok',
                                 'result': {'performed': False, 'verified': False,
                                            'observed': {'reason': 'stale_ref'}}})
        reply = events.internal_action({'action': 'result', 'taskId': 'task',
                                        'commandId': click['commandId']}, **self.paths)
        self.assertEqual(reply['nextAction'], 'done')
        events.internal_action({'action': 'command', 'taskId': 'task',
                                'operation': 'click', 'snapshotId': 'snap-1', 'ref': 3}, **self.paths)

    def test_error_result_tells_the_agent_to_stop(self):
        self.prepare_command()
        shot = events.internal_action({'action': 'command', 'taskId': 'task',
                                       'operation': 'snapshot'}, **self.paths)
        self.sync(commandResult={'requestId': shot['commandId'], 'status': 'error',
                                 'error': '缺少已核验的文档身份'})
        reply = events.internal_action({'action': 'result', 'taskId': 'task',
                                        'commandId': shot['commandId']}, **self.paths)
        self.assertEqual(reply['status'], 'error')
        self.assertEqual(reply['nextAction'], 'stop')

    def test_navigate_requires_a_plain_http_url(self):
        self.prepare_command()
        submit = lambda **extra: events.internal_action(  # noqa: E731
            dict({'action': 'command', 'taskId': 'task', 'operation': 'navigate'}, **extra), **self.paths)
        with self.assertRaises(ValueError):
            submit()
        with self.assertRaises(ValueError):
            submit(url='file:///etc/passwd')
        with self.assertRaises(ValueError):
            submit(url='https://user:secret@example.org/')
        with self.assertRaises(ValueError):
            submit(url='https://example.org/', ref=1)
        queued = submit(url='https://example.org/next')
        self.assertEqual(self.sync()['command']['params'], {'url': 'https://example.org/next'})
        self.assertTrue(queued['commandId'])

    def test_read_is_bounded_and_does_not_clear_pending_input_verification(self):
        self.prepare_command()
        capabilities = ['cdp_input', 'document_binding', 'background_popups', 'paged_read']
        self.sync(capabilities=capabilities)
        with events.event_store(self.store) as data:
            data['peer']['pendingVerify'] = {'taskId': 'task', 'commandId': 'old-input'}
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'read',
                                        'mode': 'content', 'limit': 2, 'maxChars': 4000}, **self.paths)
        result = {'readId': 'read-1', 'mode': 'content', 'url': 'https://example.org/path', 'title': 'Fixture',
                  'offset': 0, 'nextOffset': None, 'totalItems': 1, 'sourceTruncated': False, 'domOnly': True,
                  'observedAt': 1794210000000,
                  'items': [{'id': 'post-1', 'kind': 'article', 'text': 'Rendered content',
                             'textOffset': 0, 'textComplete': True}]}
        self.sync(capabilities=capabilities, commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': result})
        reply = events.internal_action({'action': 'result', 'taskId': 'task', 'commandId': queued['commandId']}, **self.paths)
        self.assertEqual(reply['result'], result)
        self.assertEqual(events.read_file(self.store)['peer']['pendingVerify']['commandId'], 'old-input')
        with self.assertRaisesRegex(ValueError, '尚未用 snapshot'):
            events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'click',
                                    'snapshotId': 'snap-1', 'ref': 1}, **self.paths)

    def test_read_requires_new_extension_and_valid_cursor_options(self):
        self.prepare_command()
        args = {'action': 'command', 'taskId': 'task', 'operation': 'read'}
        with self.assertRaisesRegex(ValueError, 'EXTENSION_UPGRADE_REQUIRED'):
            events.internal_action(args, **self.paths)
        for change in ({'mode': 'html'}, {'offset': 1}, {'limit': 101}, {'limit': True},
                       {'maxChars': 30000}, {'snapshotId': 'snapshot'}, {'readId': ''}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                events.command_params(args | change, 'read')

    def test_late_read_of_a_replaced_document_is_discarded_without_input_authorization(self):
        self.prepare_command()
        capabilities = ['cdp_input', 'document_binding', 'background_popups', 'paged_read']
        self.sync(capabilities=capabilities)
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'read'}, **self.paths)
        self.sync(capabilities=capabilities)  # Delivered for the original document.
        with events.event_store(self.store) as data:
            data['tasks']['task']['event']['documentId'] = 'replacement-document'
            data['tasks']['task']['event']['navigationEpoch'] += 1
            data['peer']['pendingVerify'] = {'taskId': 'task', 'commandId': 'prior-input'}
        self.sync(capabilities=capabilities, commandResult={'requestId': queued['commandId'], 'status': 'ok',
                  'result': {'syntheticOldDocument': True}})
        reply = events.internal_action({'action': 'result', 'taskId': 'task', 'commandId': queued['commandId']}, **self.paths)
        self.assertEqual(reply['status'], 'error')
        self.assertEqual(reply['errorCode'], 'STALE_DOCUMENT')
        self.assertNotIn('result', reply)
        self.assertEqual(events.read_file(self.store)['peer']['pendingVerify']['commandId'], 'prior-input')

    def test_read_result_rejects_oversized_or_inconsistent_page_evidence(self):
        result = {'readId': 'read-1', 'mode': 'links', 'url': 'https://example.org/path', 'title': 'Fixture',
                  'offset': 0, 'nextOffset': None, 'totalItems': 1, 'sourceTruncated': False, 'domOnly': True,
                  'observedAt': 1794210000000, 'items': [{'name': 'Topic', 'href': 'https://example.org/t/1',
                                                       'urlHasQuery': False, 'urlHasFragment': False}]}
        params = {'mode': 'links', 'readId': 'read-1'}
        self.assertEqual(events.validate_read(result, params), result)
        for change in ({'readId': 'other'}, {'offset': 1}, {'totalItems': 2}, {'nextOffset': 0},
                       {'domOnly': False}, {'extra': 'x'},
                       {'items': [result['items'][0] | {'href': 'https://u:p@example.org/t/1'}]},
                       {'items': [result['items'][0] | {'href': 'https://example.org/t/1?secret=1'}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                events.validate_read(result | change, params)

    def test_snapshot_allows_only_redacted_link_destinations(self):
        element = self.SNAPSHOT['elements'][0] | {'href': 'https://example.org/t/topic/1',
                                                  'urlHasQuery': True, 'urlHasFragment': False}
        self.assertEqual(events.validate_snapshot(self.SNAPSHOT | {'elements': [element]})['elements'][0], element)
        for change in ({'href': 'javascript:alert(1)'}, {'href': 'https://example.org/t/1?secret=x'},
                       {'urlHasQuery': 1}, {'downloadPath': '/tmp/a'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                events.validate_snapshot(self.SNAPSHOT | {'elements': [element | change]})

    def test_snapshot_coverage_is_optional_but_must_match_returned_elements(self):
        coverage = {'returnedElements': len(self.SNAPSHOT['elements']), 'limit': 300, 'truncated': False,
                    'scope': 'visible_interactive_top_and_accessible_frames',
                    'elementCountScope': 'top_document_dom_nodes_capped_10000'}
        snapshot = self.SNAPSHOT | {'coverage': coverage}
        self.assertEqual(events.validate_snapshot(snapshot), snapshot)
        self.assertEqual(events.validate_snapshot(self.SNAPSHOT), self.SNAPSHOT)
        for change in ({'returnedElements': 300}, {'returnedElements': True}, {'limit': 301},
                       {'truncated': True}, {'truncated': 1}, {'scope': 'all_dom'},
                       {'elementCountScope': 'all_frames'}, {'extra': 'untrusted'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                events.validate_snapshot(snapshot | {'coverage': coverage | change})
        elements = [self.SNAPSHOT['elements'][0] | {'ref': index + 1} for index in range(300)]
        for truncated in (False, True):
            result = snapshot | {'elements': elements, 'coverage': coverage |
                                 {'returnedElements': 300, 'truncated': truncated}}
            self.assertEqual(events.validate_snapshot(result), result)

    def test_cancel_before_delivery_reports_it_may_not_have_executed(self):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                         'operation': 'click', 'snapshotId': 'snap-1', 'ref': 3}, **self.paths)
        cancel = {'action': 'cancel', 'taskId': 'task', 'commandId': queued['commandId']}
        reply = events.internal_action(cancel, **self.paths)
        self.assertEqual(reply['status'], 'canceled')
        self.assertFalse(reply['mayHaveExecuted'])
        self.assertIsNone(self.sync()['command'])
        poll = events.internal_action({'action': 'result', 'taskId': 'task',
                                       'commandId': queued['commandId']}, **self.paths)
        self.assertEqual(poll['status'], 'canceled')
        self.assertEqual(events.internal_action(cancel, **self.paths)['status'], 'canceled')

    def test_delivered_command_cancel_is_honest_about_side_effects(self):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                         'operation': 'click', 'snapshotId': 'snap-1', 'ref': 3}, **self.paths)
        self.assertEqual(self.sync()['command']['requestId'], queued['commandId'])
        reply = events.internal_action({'action': 'cancel', 'taskId': 'task',
                                        'commandId': queued['commandId']}, **self.paths)
        self.assertTrue(reply['mayHaveExecuted'])
        # 迟到结果不采信，也不覆盖成成功。
        self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok',
                                 'result': {'performed': True, 'verified': False,
                                            'observed': {'tag': 'a'}}})
        poll = events.internal_action({'action': 'result', 'taskId': 'task',
                                       'commandId': queued['commandId']}, **self.paths)
        self.assertEqual(poll['status'], 'canceled')
        self.assertNotIn('result', poll)

    def test_status_exposes_recovery_state_for_commands(self):
        self.prepare_command()
        self.assertFalse(self.request({'action': 'status', 'taskId': 'task'})['status']['commandState']['verificationRequired'])
        click = events.internal_action({'action': 'command', 'taskId': 'task',
                                        'operation': 'click', 'snapshotId': 'snap-1', 'ref': 3}, **self.paths)
        state = self.request({'action': 'status', 'taskId': 'task'})['status']['commandState']
        self.assertEqual(state['pendingOperation'], 'click')
        self.assertFalse(state['verificationRequired'])
        self.sync(commandResult={'requestId': click['commandId'], 'status': 'ok',
                                 'result': {'performed': True, 'verified': False,
                                            'observed': {'tag': 'a'}}})
        state = self.request({'action': 'status', 'taskId': 'task'})['status']['commandState']
        self.assertTrue(state['verificationRequired'])
        self.assertEqual(state['verificationOperation'], 'click')
        self.assertEqual(state['lastResultStatus'], 'ok')

    DIAGNOSTICS = {'url': 'https://example.org/path', 'readyState': 'complete',
                   'errors': [{'message': 'TypeError: x is undefined', 'at': 1761900000000}],
                   'viewport': {'width': 1280, 'height': 800},
                   'scroll': {'x': 0, 'y': 240}}

    def test_diagnostics_uses_the_verification_path(self):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                         'operation': 'diagnostics'}, **self.paths)
        self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok',
                                 'result': self.DIAGNOSTICS})
        reply = events.internal_action({'action': 'result', 'taskId': 'task',
                                        'commandId': queued['commandId']}, **self.paths)
        self.assertEqual(reply['status'], 'ok')
        self.assertEqual(reply['result']['errors'][0]['message'], 'TypeError: x is undefined')
        self.assertEqual(reply['nextAction'], 'observe')
        again = events.internal_action({'action': 'command', 'taskId': 'task',
                                        'operation': 'diagnostics'}, **self.paths)
        rejected = self.sync(commandResult={'requestId': again['commandId'], 'status': 'ok',
                                           'result': self.DIAGNOSTICS | {'cookie': 'x'}})
        self.assertEqual(rejected['commandAck']['code'], 'INVALID_BROWSER_RESULT')
        self.assertNotIn('cookie', self.store.read_text())
        self.assertEqual(events.status('task', **self.paths)['nativeConnection'], 'connected')

    def test_annotation_registry_is_per_task_and_deduplicates_by_ref(self):
        self.prepare_assignment()
        add = lambda **extra: events.internal_action(  # noqa: E731
            dict({'action': 'annotate', 'taskId': 'task', 'ref': 5,
                  'note': '按钮文案需要更明确'}, **extra), **self.paths)
        first = add()
        self.assertTrue(first['annotated'])
        self.assertEqual(first['count'], 1)
        add(note='同一元素的新备注')
        listing = events.internal_action({'action': 'annotations', 'taskId': 'task'}, **self.paths)
        self.assertEqual(len(listing['annotations']), 1)
        self.assertEqual(listing['annotations'][0]['note'], '同一元素的新备注')
        with self.assertRaises(ValueError):
            add(ref=0)
        with self.assertRaises(ValueError):
            add(note='   ')
        with self.assertRaises(ValueError):
            add(note='x' * 201)

    def test_cancel_on_finished_command_reports_completed(self):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                         'operation': 'snapshot'}, **self.paths)
        self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok',
                                 'result': self.SNAPSHOT})
        reply = events.internal_action({'action': 'cancel', 'taskId': 'task',
                                        'commandId': queued['commandId']}, **self.paths)
        self.assertEqual(reply['status'], 'completed')
        self.assertEqual(reply['operation'], 'snapshot')
        # 结果不受 cancel 影响，仍可正常取回。
        still = events.internal_action({'action': 'result', 'taskId': 'task',
                                        'commandId': queued['commandId']}, **self.paths)
        self.assertEqual(still['status'], 'ok')

    def test_results_are_retained_for_recent_commands_and_evictions_are_explicit(self):
        self.prepare_command()
        ids = []
        for _ in range(events.MAX_RESULTS + 2):
            queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                             'operation': 'snapshot'}, **self.paths)
            ids.append(queued['commandId'])
            self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok',
                                     'result': self.SNAPSHOT})
        poll = lambda cid: events.internal_action(  # noqa: E731
            {'action': 'result', 'taskId': 'task', 'commandId': cid}, **self.paths)
        self.assertEqual(poll(ids[0])['status'], 'evicted')
        self.assertEqual(poll(ids[-1])['status'], 'ok')
        self.assertEqual(poll(ids[-1])['status'], 'ok', '重复轮询必须一致')
        self.assertEqual(poll('f' * 32)['status'], 'unknown')

    def test_ping_keeps_the_port_alive_without_touching_state(self):
        before = self.store.read_bytes() if self.store.exists() else None
        reply = self.request({'action': 'ping'})
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['protocolVersion'], 2)
        self.assertIsInstance(reply['heartbeatAt'], float)
        with self.assertRaises(ValueError):
            self.request({'action': 'ping', 'taskId': 'task'})
        self.assertEqual(self.store.read_bytes() if self.store.exists() else None, before,
                         'ping 不得写入观察存储')

    def test_disable_drops_queued_command_and_never_replays_after_enable(self):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                         'operation': 'snapshot'}, **self.paths)
        self.assertEqual(self.sync()['command']['requestId'], queued['commandId'])
        self.sync(enabled=False)
        self.assertIsNone(self.sync()['command'], '停用后不得继续下发')
        self.assertIsNone(self.sync(enabled=True)['command'], '重新启用不得重放旧命令')
        reply = events.internal_action({'action': 'result', 'taskId': 'task',
                                        'commandId': queued['commandId']}, **self.paths)
        self.assertEqual(reply['status'], 'unknown', '旧命令不得伪装成已完成')

    def test_verify_mapping_requires_a_unique_title_match(self):
        self.prepare_assignment()
        self.sync(assignmentTitle='Example Domain')
        call = lambda windows: events.internal_action(  # noqa: E731
            {'action': 'verify_mapping', 'taskId': 'task', 'windows': windows}, **self.paths)
        self.assertFalse(call([{'windowId': 1, 'title': 'Example Domain'},
                               {'windowId': 2, 'title': 'Example Domain'}])['verified'],
                         '重名窗口不得视为已映射')
        self.assertEqual(call([{'windowId': 3, 'title': 'Other'}])['reason'], 'no_match')
        hit = call([{'windowId': 9, 'title': ' Example Domain '}])
        self.assertTrue(hit['verified'])
        self.assertEqual(hit['windowId'], 9)
        record = events.status('task', **self.paths)
        self.assertTrue(record['mappingVerified'])
        self.assertEqual(record['mapping']['method'], 'title')
        with self.assertRaises(ValueError):
            events.internal_action({'action': 'verify_mapping', 'taskId': 'task'}, **self.paths)

    def test_verify_mapping_accepts_chrome_window_title_decoration(self):
        self.prepare_assignment()
        self.sync(assignmentTitle='QuietBridge Soak Step 5')
        call = lambda windows: events.internal_action(  # noqa: E731
            {'action': 'verify_mapping', 'taskId': 'task', 'windows': windows}, **self.paths)
        decorated = 'QuietBridge Soak Step 5 - Google Chrome - 不务正业的程序员'
        hit = call([{'windowId': 41, 'title': decorated}])
        self.assertTrue(hit['verified'], 'macOS Chrome 窗口标题后缀不得阻止映射')
        self.assertEqual(hit['match'], 'prefixed')
        self.assertEqual(hit['windowId'], 41)
        self.assertEqual(events.status('task', **self.paths)['mapping']['match'], 'prefixed')
        self.assertEqual(
            call([{'windowId': 42, 'title': 'QuietBridge Soak Step 50 - Google Chrome'}])['reason'],
            'no_match', '编号前缀相同但页面标题不同不得命中')
        self.assertEqual(
            call([{'windowId': 46, 'title': 'Archive - QuietBridge Soak Step 5'}])['reason'],
            'no_match', '标签标题出现在窗口标题中段不得命中')
        self.assertFalse(
            call([{'windowId': 43, 'title': 'QuietBridge Soak Step 5 - Google Chrome'},
                  {'windowId': 44, 'title': 'QuietBridge Soak Step 5 - Google Chrome - P2'}])['verified'],
            '多个前缀命中仍须报歧义')
        exact = call([{'windowId': 45, 'title': ' QuietBridge Soak Step 5 '}])
        self.assertTrue(exact['verified'])
        self.assertEqual(exact['match'], 'exact')

    def test_window_mapping_uses_active_tab_metadata_without_changing_task_tab(self):
        self.prepare_assignment()
        assignment = self.sync()['assignment']
        evidence = {'bindingId': assignment['bindingId'], 'browserWindowId': 2,
                    'tabId': 8, 'title': 'Active document'}
        self.sync(assignmentTitle='Inactive task document', windowEvidence=evidence)
        args = {'action': 'verify_mapping', 'taskId': 'task',
                'windows': [{'windowId': 900, 'title': 'Active document'}]}
        result = events.internal_action(args, **self.paths)
        self.assertTrue(result['verified'])
        self.assertEqual(result['method'], 'window_active_title')
        self.assertFalse(result['targetTabActiveAtVerification'])
        self.assertEqual(result['windowActiveTabId'], 8)
        self.assertEqual(self.sync()['assignment']['tabId'], 7)
        duplicate = args | {'windows': args['windows'] + [{'windowId': 901, 'title': 'Active document'}]}
        self.assertEqual(events.internal_action(duplicate, **self.paths)['reason'], 'ambiguous')
        with events.event_store(self.store) as data:
            data['peer']['windowEvidence']['at'] -= 21
        self.assertFalse(events.internal_action(args, **self.paths)['verified'])
        for change in ({'bindingId': '0' * 32}, {'browserWindowId': 3}):
            self.sync(windowEvidence=evidence | change)
            self.assertNotIn('windowEvidence', events.read_file(self.store)['peer'])
            self.assertFalse(events.internal_action(args, **self.paths)['verified'])
        self.sync(windowEvidence=evidence)
        self.sync(windowEvidence=None)
        self.assertFalse(events.internal_action(args, **self.paths)['verified'])


    def test_equal_window_titles_use_unique_geometry_without_renaming_or_navigation(self):
        self.prepare_assignment()
        assignment = self.sync()['assignment']
        bounds = {'x': 120, 'y': 39, 'width': 1800, 'height': 1048}
        evidence = {'bindingId': assignment['bindingId'], 'browserWindowId': 2,
                    'tabId': 7, 'title': 'LINUX DO', 'bounds': bounds}
        self.sync(windowEvidence=evidence)
        windows = [{'windowId': 900, 'title': 'LINUX DO', 'bounds': bounds},
                   {'windowId': 901, 'title': 'LINUX DO', 'bounds': dict(bounds, x=11161)}]
        args = {'action': 'verify_mapping', 'taskId': 'task', 'windows': windows}
        reply = events.internal_action(args, **self.paths)
        self.assertTrue(reply['verified'])
        self.assertEqual(reply['windowId'], 900)
        self.assertEqual(reply['method'], 'window_active_title_and_bounds')
        self.assertIsNone(events.read_file(self.store)['peer'].get('command'))
        windows[1]['bounds'] = bounds
        self.assertEqual(events.internal_action(args, **self.paths)['reason'], 'ambiguous')
        windows[1].pop('bounds')
        self.assertEqual(events.internal_action(args, **self.paths)['reason'], 'ambiguous',
                         'Missing geometry cannot prove that the second window differs')

    def test_window_geometry_requires_all_coordinates_and_current_binding(self):
        self.prepare_assignment()
        assignment = self.sync()['assignment']
        bounds = {'x': 120, 'y': 39, 'width': 1800, 'height': 1048}
        evidence = {'bindingId': assignment['bindingId'], 'browserWindowId': 2, 'tabId': 7,
                    'title': 'LINUX DO', 'bounds': bounds}
        for invalid in (dict(bounds, width=-1), dict(bounds, x=float('nan')), dict(bounds, height=True), {'width': 1800}):
            with self.assertRaisesRegex(ValueError, '几何'):
                self.sync(windowEvidence=dict(evidence, bounds=invalid))
        self.sync(assignmentTitle='LINUX DO', windowEvidence=evidence)
        with events.event_store(self.store) as data:
            data['peer']['windowEvidence']['at'] -= 21
        args = {'action': 'verify_mapping', 'taskId': 'task', 'windows': [
            {'windowId': 900, 'title': 'LINUX DO', 'bounds': bounds},
            {'windowId': 901, 'title': 'LINUX DO', 'bounds': dict(bounds, x=11161)}]}
        self.assertFalse(events.internal_action(args, **self.paths)['verified'])

    def test_foreign_extension_command_error_has_explicit_recovery_code(self):
        self.prepare_command()
        command = events.internal_action({'action': 'command', 'taskId': 'task',
            'operation': 'click', 'snapshotId': 'snap-1', 'ref': 1}, **self.paths)
        self.sync()
        self.sync(commandResult={'requestId': command['commandId'], 'status': 'error',
            'error': 'Cannot access a chrome-extension:// URL of different extension'})
        result = events.internal_action({'action': 'result', 'taskId': 'task',
            'commandId': command['commandId']}, **self.paths)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['errorCode'], 'BROWSER_DEBUGGER_RESTRICTED')
        self.assertEqual(result['nextAction'], 'stop')

    def test_debugger_failure_preserves_original_reason_and_only_bounded_frame_identity(self):
        self.prepare_command()
        for reason, message, code in (
                ('foreign_extension_page', 'Cannot access a chrome-extension:// URL of different extension', 'BROWSER_DEBUGGER_RESTRICTED'),
                ('debugger_in_use', 'Another debugger is already attached to the tab with id: 7.', 'BROWSER_DEBUGGER_IN_USE')):
            command = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'}, **self.paths)
            evidence = {'reason': reason, 'stage': 'attach', 'chromeMessage': message,
                'foreignFrameExtensionIds': ['a' * 32] if reason == 'foreign_extension_page' else [],
                'frameEvidence': 'top_document_iframe_src' if reason == 'foreign_extension_page' else 'not_checked'}
            self.sync(commandResult={'requestId': command['commandId'], 'status': 'error',
                'error': code + '：attach 被拒绝', 'debuggerFailure': evidence})
            result = events.internal_action({'action': 'result', 'taskId': 'task',
                'commandId': command['commandId']}, **self.paths)
            self.assertEqual(result['errorCode'], code)
            self.assertEqual(result['debuggerFailure'], evidence)
            self.assertEqual(result['nextAction'], 'stop')

    def test_malformed_debugger_details_are_rejected_without_poisoning_channel(self):
        self.prepare_command()
        evidence = {'reason': 'foreign_extension_page', 'stage': 'attach',
            'chromeMessage': 'Cannot access a chrome-extension:// URL of different extension',
            'foreignFrameExtensionIds': ['a' * 32], 'frameEvidence': 'top_document_iframe_src'}
        for change in ({'data': 'pixels'}, {'chromeMessage': 'https://private.example/?secret=1'},
                       {'foreignFrameExtensionIds': ['chrome-extension://' + 'a' * 32 + '/?secret=1']},
                       {'frameEvidence': 'not_checked'}, {'stage': 'attach\nunsafe'}):
            command = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'}, **self.paths)
            reply = self.sync(commandResult={'requestId': command['commandId'], 'status': 'error',
                'error': 'BROWSER_DEBUGGER_RESTRICTED：attach 被拒绝', 'debuggerFailure': evidence | change})
            self.assertEqual(reply['commandAck']['code'], 'INVALID_BROWSER_RESULT')
            stored = events.read_file(self.store)['peer']['commandResults'][-1]
            self.assertNotIn('debuggerFailure', stored)
            self.assertEqual(self.sync()['taskAssignment'], 'assigned')

    def test_new_task_does_not_inherit_old_commands_snapshots_or_verification(self):
        self.prepare_command()
        old = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'navigate',
                                     'url': 'https://example.org/old'}, **self.paths)
        self.sync()  # Delivered but outcome unknown; do not replay it.
        with events.event_store(self.store) as data:
            data['peer']['pendingVerify'] = {'taskId': 'task', 'operation': 'navigate', 'commandId': old['commandId']}
        tasks = events.read_file(self.tasks)
        previous = tasks['tasks']['task']
        previous['state'] = 'ended'
        tasks['tasks']['fresh'] = dict(previous, taskId='fresh', state='bound')
        tasks['activeTaskId'] = 'fresh'
        self.tasks.write_text(json.dumps(tasks))
        reply = events.internal_action({'action': 'assign', 'taskId': 'fresh', 'tabId': 7,
                                       'browserWindowId': 2}, **self.paths)
        self.assertTrue(reply['assigned'])
        peer = events.read_file(self.store)['peer']
        self.assertIsNone(peer.get('pendingVerify'))
        self.assertIsNone(peer.get('snapshot'))
        self.assertIsNone(peer.get('command'))
        terminal = next(t for t in peer['commandTerminals'] if t['commandId'] == old['commandId'])
        self.assertTrue(terminal['mayHaveExecuted'])
        self.assertEqual(terminal['taskId'], 'task')
        self.assertEqual(self.sync(commandResult={'requestId': old['commandId'], 'status': 'error',
                                                'error': 'late old result'})['commandAck']['status'], 'discarded')
        assignment = self.sync()['assignment']
        event = self.event()
        event.update(bindingId=assignment['bindingId'])
        self.request({'action': 'event', 'taskId': 'fresh', 'token': assignment['token'], 'event': event})
        queued = events.internal_action({'action': 'command', 'taskId': 'fresh', 'operation': 'navigate',
                                        'url': 'https://example.org/next'}, **self.paths)
        self.assertEqual(queued['status'], 'queued', 'New navigation needs no old-task snapshot')

    def test_same_task_reassignment_preserves_unknown_action_barrier(self):
        self.prepare_command()
        command = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'navigate',
                                         'url': 'https://example.org/next'}, **self.paths)
        self.sync()
        events.internal_action({'action': 'assign', 'taskId': 'task', 'tabId': 7, 'browserWindowId': 2}, **self.paths)
        peer = events.read_file(self.store)['peer']
        self.assertEqual(peer['pendingVerify']['taskId'], 'task')
        self.assertEqual(peer['pendingVerify']['operation'], 'reassign')

    def test_known_attach_rejection_does_not_require_snapshot_before_next_navigation(self):
        self.prepare_command()
        command = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'navigate',
                                         'url': 'https://example.org/next'}, **self.paths)
        self.sync(commandResult={'requestId': command['commandId'], 'status': 'error',
            'error': 'BROWSER_DEBUGGER_RESTRICTED：attach 被拒绝', 'debuggerFailure': {
                'reason': 'foreign_extension_page', 'stage': 'attach',
                'chromeMessage': 'Cannot access a chrome-extension:// URL of different extension',
                'foreignFrameExtensionIds': [], 'frameEvidence': 'not_checked', 'inputDispatched': False}})
        self.assertIsNone(events.read_file(self.store)['peer'].get('pendingVerify'))
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'navigate',
                                        'url': 'https://example.org/next'}, **self.paths)
        self.assertEqual(queued['status'], 'queued')

    def test_child_attach_failure_after_input_still_requires_observation(self):
        self.prepare_command()
        command = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'click',
                                         'snapshotId': 'snap-1', 'ref': 1}, **self.paths)
        self.sync(commandResult={'requestId': command['commandId'], 'status': 'error',
            'error': 'BROWSER_DEBUGGER_RESTRICTED：attach 被拒绝', 'debuggerFailure': {
                'reason': 'foreign_extension_page', 'stage': 'attach',
                'chromeMessage': 'Cannot access a chrome-extension:// URL of different extension',
                'foreignFrameExtensionIds': [], 'frameEvidence': 'not_checked'}})
        self.assertEqual(events.read_file(self.store)['peer']['pendingVerify']['commandId'], command['commandId'])

    def test_old_navigation_experiment_report_is_discarded_without_authorizing_capture(self):
        self.prepare_command()
        with events.event_store(self.store) as data:
            data['peer']['windowPreview'] = {'targetOnCurrentSpace': False}
            data['peer']['navigationCapture'] = {'status': 'captured'}
        reply = self.sync(navigationCapture={'old': 'ignored'})
        self.assertNotIn('windowPreview', reply)
        self.assertNotIn('navigationCapture', events.status('task', **self.paths))
        self.assertNotIn('windowPreview', events.read_file(self.store)['peer'])

    def test_core_actions_and_repeated_navigation_keep_one_binding_without_experiments(self):
        self.prepare_command()
        initial = events.read_file(self.store)['tasks']['task']
        token, binding = initial['token'], initial['bindingId']
        self.sync(assignmentTitle='Example Domain')
        self.assertTrue(events.internal_action({'action': 'verify_mapping', 'taskId': 'task',
            'windows': [{'windowId': 900, 'title': 'Example Domain'}]}, **self.paths)['verified'])

        def complete(operation, result, **params):
            queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                            'operation': operation, **params}, **self.paths)
            self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': result})
            reply = events.internal_action({'action': 'result', 'taskId': 'task',
                                            'commandId': queued['commandId']}, **self.paths)
            self.assertEqual(reply['status'], 'ok')
            return reply

        for operation, params in (
                ('fill', {'snapshotId': 'snap-1', 'ref': 1, 'value': 'core-check'}),
                ('click', {'snapshotId': 'snap-1', 'ref': 1}),
                ('scroll', {'snapshotId': 'snap-1', 'direction': 'down', 'lines': 6})):
            complete(operation, {'performed': True, 'verified': False, 'observed': {}}, **params)
            complete('snapshot', self.SNAPSHOT)
        for epoch in (2, 3, 4):
            url = 'https://example.org/step-' + str(epoch)
            complete('navigate', {'performed': True, 'verified': False, 'observed': {}}, url=url)
            event = dict(self.event(sequence=epoch * 2), documentId='doc-' + str(epoch), navigationEpoch=epoch,
                         url=url, phase='content', readyState='complete', domReady=True, loaded=True)
            self.request({'action': 'event', 'taskId': 'task', 'token': token,
                          'event': dict(event, phase='committed', sequence=epoch * 2 - 1,
                                        readyState=None, domReady=False, loaded=False,
                                        bodyPresent=False, bodyVisible=False, elementCount=0,
                                        conditionMatched=None, contentReady=None)})
            self.request({'action': 'event', 'taskId': 'task', 'token': token, 'event': event})
            complete('diagnostics', {'url': url, 'readyState': 'complete', 'errors': [],
                                    'viewport': {'width': 800, 'height': 600}, 'scroll': {'x': 0, 'y': 0}})
            state = events.status('task', **self.paths)
            self.assertFalse(state['commandState']['verificationRequired'])
            self.assertEqual(state['bindingId'], binding)
        tasks = events.read_file(self.tasks)
        tasks['tasks']['task']['state'] = 'ended'
        tasks['activeTaskId'] = None
        self.tasks.write_text(json.dumps(tasks))
        sync = self.sync()
        self.assertIsNone(sync['assignment'])
        self.assertIsNone(sync['command'])
        self.assertIsNone(sync['previewLease'])

    def test_mapping_evidence_is_host_local_and_does_not_break_observer_assignment(self):
        self.prepare_assignment()
        self.sync(assignmentTitle='Example Domain')
        events.internal_action({'action': 'verify_mapping', 'taskId': 'task',
                                'windows': [{'windowId': 900, 'title': 'Example Domain'}]}, **self.paths)
        wire = self.sync()['assignment']
        # Observer messages cannot attest to the WindowServer mapping. Its wire
        # flag must stay false while the host retains independent true evidence.
        self.assertIs(wire['mappingVerified'], False)
        self.assertNotIn('mapping', wire)
        local = events.status('task', **self.paths)
        self.assertTrue(local['mappingVerified'])
        self.assertTrue(local['crossCheck']['consistent'])
        latest = self.event(sequence=2)
        self.assertTrue(self.request({'action': 'event', 'taskId': 'task',
                                     'token': wire['token'], 'event': latest})['ok'])
        queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                         'operation': 'snapshot'}, **self.paths)
        self.assertEqual(queued['status'], 'queued')

    def test_mismatched_mapping_blocks_commands_and_shows_in_status(self):
        self.prepare_command()
        self.sync(assignmentTitle='Example Domain')
        events.internal_action({'action': 'verify_mapping', 'taskId': 'task',
                                'windows': [{'windowId': 900, 'title': 'Example Domain'}]}, **self.paths)
        state = events.status('task', **self.paths)
        self.assertTrue(state['mappingVerified'])
        self.assertTrue(state['crossCheck']['consistent'], '映射窗口与绑定窗口一致')
        # 人为把映射改到别的窗口，命令必须被拒。
        data = events.read_file(self.store)
        data['tasks']['task']['mapping'] = {'windowId': 123, 'method': 'title',
                                            'title': 'Example Domain', 'at': 1}
        self.store.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            events.internal_action({'action': 'command', 'taskId': 'task',
                                    'operation': 'snapshot'}, **self.paths)
        state = events.status('task', **self.paths)
        self.assertFalse(state['crossCheck']['consistent'])
        self.assertEqual(state['crossCheck']['boundWindowId'], 900)

    def test_legacy_bind_rejected_without_task_or_store_access(self):
        message = {'action': 'bind', 'taskId': 'task', 'confirm': True,
                   'tabId': 7, 'browserWindowId': 2, 'authorizedOrigin': 'https://example.org', 'selector': ''}
        original = self.tasks.read_bytes()
        with mock.patch.object(events, 'task_exists', side_effect=AssertionError('不得读取任务')), \
                mock.patch.object(events, 'event_store', side_effect=AssertionError('不得打开存储')):
            for request in (message, {'action': 'bind'}, message | {'taskId': 'missing'},
                            message | {'confirm': False}):
                result = self.request(request)
                self.assertFalse(result['ok'])
                self.assertEqual(result['code'], 'LEGACY_UNSUPPORTED')
                self.assertIn('legacy bind unsupported', result['error'])
                self.assertIn('内部 assign', result['error'])
        self.assertEqual(self.tasks.read_bytes(), original)
        self.assertFalse(self.store.exists())
        self.assertFalse(Path(str(self.store) + '.lock').exists())

    def test_persisted_legacy_binding_cannot_report_events_or_resume(self):
        token = self.assign()
        with events.event_store(self.store) as data:
            record = data['tasks']['task']
            for key in ('globalOrigins', 'targetIdentity', 'assignmentVersion'):
                record.pop(key)
        before = self.store.read_bytes()
        for action in ('event', 'unbind'):
            message = {'action': action, 'taskId': 'task', 'token': token}
            if action == 'event':
                message['event'] = self.event()
            with self.assertRaisesRegex(ValueError, 'legacy assignment unsupported'):
                self.request(message)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertFalse(events.status('task', **self.paths)['paired'])
        self.assertIsNone(self.sync()['assignment'])
        new_token = self.assign()
        self.assertNotEqual(new_token, token)
        self.assertTrue(self.request({'action': 'event', 'taskId': 'task', 'token': new_token,
                                      'event': self.event()})['ok'])

    def test_missing_task_invalid_origin_and_fields_do_not_write(self):
        message = {'action': 'event', 'taskId': 'missing', 'token': 'a' * 64, 'event': self.event()}
        for change in ({}, {'taskId': []}, {'taskId': 'task', 'extra': 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.request(message | change)
        for origin in ('https://linux.do', 'chrome-extension://' + 'b' * 32 + '/', None):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                self.request({'action': 'status', 'taskId': 'task'}, origin=origin)
        self.assertFalse(self.store.exists())

    def test_task_scope_token_and_replay(self):
        token = self.assign()
        with self.assertRaises(ValueError):
            self.assign('other')
        with self.assertRaises(ValueError):
            events.internal_action({'action': 'assign', 'taskId': 'task', 'tabId': True,
                                   'browserWindowId': 2}, **self.paths)
        message = {'action': 'event', 'taskId': 'task', 'token': token, 'event': self.event()}
        for update in ({'taskId': 'other'}, {'token': 'x' * 64},
                       {'event': self.event() | {'tabId': 8}}):
            with self.assertRaises(ValueError):
                self.request(message | update)
        self.request(message)
        with self.assertRaises(ValueError):
            self.request(message)
        with self.assertRaises(ValueError):
            self.request(message | {'event': self.event(2) | {'navigationEpoch': 0}})

    def test_strict_event_schema_and_privacy(self):
        for change in ({'mappingVerified': True}, {'bodyText': '不应收集'}, {'loaded': 1},
                       {'elementCount': 10001}, {'sequence': True}, {'phase': []},
                       {'documentId': {}}, {'url': 'https://u:p@example.org/t/1'},
                       {'url': 'https://example.org/t/1?secret=x'}, {'url': 'https://example.org/#x'},
                       {'url': 'file:///tmp/a'}, {'contentReady': False}, {'bindingId': 'old'}):
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                events.validate_event(self.event() | change)

    def test_trusted_binding_origin_and_lifecycle_are_immutable(self):
        token = self.assign()
        record = events.status('task', **self.paths)
        self.assertEqual(record['authorizedOrigin'], 'https://example.org')
        self.assertFalse(record['mappingVerified'])
        def send(event):
            return self.request({'action': 'event', 'taskId': 'task', 'token': token, 'event': event})
        first = self.event()
        send(first)
        for change in ({'url': 'https://chromewebstore.google.com/detail/x'},
                       {'url': 'chrome://settings'}, {'url': 'https://example.org/path?secret=x'},
                       {'authorizedOrigin': 'https://other.example'}, {'documentId': 'old'},
                       {'bindingId': 'b' * 32}, {'navigationEpoch': 2}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                send(first | {'sequence': 2} | change)
        paused = first | {'sequence': 2, 'navigationEpoch': 2, 'phase': 'paused',
            'documentId': None, 'url': None, 'loaded': False, 'domReady': False,
            'readyState': None, 'bodyPresent': False, 'bodyVisible': False,
            'elementCount': 0, 'conditionMatched': None}
        send(paused)
        self.assertTrue(events.status('task', **self.paths)['paused'])
        with self.assertRaises(ValueError):
            send(first | {'sequence': 3, 'navigationEpoch': 3, 'phase': 'committed'})
        self.request({'action': 'unbind', 'taskId': 'task', 'token': token})
        new_token = self.assign()
        self.assertNotEqual(token, new_token)
        with self.assertRaises(ValueError):
            self.request({'action': 'event', 'taskId': 'task', 'token': new_token, 'event': first})

    def test_navigation_epoch_document_and_error_lifecycle(self):
        token = self.assign()
        def send(event):
            self.request({'action': 'event', 'taskId': 'task', 'token': token, 'event': event})
        first = self.event()
        send(first)
        empty = first | {'loaded': False, 'domReady': False, 'readyState': None,
            'bodyPresent': False, 'bodyVisible': False, 'elementCount': 0, 'conditionMatched': None}
        before = empty | {'phase': 'before', 'documentId': None, 'navigationEpoch': 2, 'sequence': 2}
        send(before)
        committed = empty | {'phase': 'committed', 'documentId': 'new-doc', 'navigationEpoch': 2, 'sequence': 3}
        send(committed)
        with self.assertRaises(ValueError):
            send(first | {'navigationEpoch': 2, 'sequence': 4})
        send(first | {'documentId': 'new-doc', 'navigationEpoch': 2, 'sequence': 4})
        error = empty | {'phase': 'error', 'documentId': 'new-doc', 'navigationEpoch': 2, 'sequence': 5}
        send(error)
        with self.assertRaises(ValueError):
            send(first | {'documentId': 'new-doc', 'navigationEpoch': 2, 'sequence': 6})
        send(before | {'navigationEpoch': 3, 'sequence': 6})

    def test_explicit_origin_and_restricted_selector_required(self):
        for value in ('https://u:p@example.org', 'file://host', 'chrome://settings',
                      'https://example.org/path', 'https://example.org?x', 'https://example.org:443'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                events.authorized_origin(value)
        for value in ('http://localhost:8080', 'https://example.org', 'http://127.0.0.1'):
            self.assertEqual(events.authorized_origin(value), value)
        for value in ('a,b', '[value]', ':has(input)', 'div span', '*', '#x > a'):
            self.assertFalse(events.valid_selector(value))
        token = self.assign()
        with self.assertRaises(ValueError):
            self.request({'action': 'event', 'taskId': 'task', 'token': token,
                          'event': self.event() | {'conditionMatched': True}})
        self.request({'action': 'event', 'taskId': 'task', 'token': token, 'event': self.event()})

    def test_optional_css_event_validation_with_isolated_synthetic_condition(self):
        token = self.assign()
        # 只在临时 fixture 中配置条件，覆盖既有校验；不新增生产分配入口。
        with events.event_store(self.store) as data:
            data['tasks']['task']['selector'] = 'main.ready'
        message = {'action': 'event', 'taskId': 'task', 'token': token, 'event': self.event()}
        with self.assertRaises(ValueError):
            self.request(message)
        self.request(message | {'event': self.event() | {'conditionMatched': True}})
        self.assertTrue(events.status('task', **self.paths)['event']['conditionMatched'])

    def test_atomic_failure_preserves_original(self):
        token = self.assign()
        original = self.store.read_bytes()
        with mock.patch.object(events.os, 'replace', side_effect=OSError('模拟失败')):
            with self.assertRaises(OSError):
                self.request({'action': 'event', 'taskId': 'task', 'token': token, 'event': self.event()})
        self.assertEqual(self.store.read_bytes(), original)
        self.assertEqual(list(self.base.glob('*.tmp')), [])

    def test_symlink_store_lock_task_and_parent_rejected(self):
        target = self.base / 'target'
        target.write_text('{}')
        for name in (self.store, Path(str(self.store) + '.lock')):
            if name.exists():
                name.unlink()
            name.symlink_to(target)
            with self.assertRaises((OSError, ValueError)):
                self.assign()
            name.unlink()
        self.tasks.unlink()
        self.tasks.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            self.assign()
        folder = self.base / 'folder'
        folder.mkdir()
        link = self.base / 'link'
        link.symlink_to(folder, target_is_directory=True)
        with self.assertRaises(OSError):
            events.read_file(link / 'x')
        self.assertEqual(target.read_text(), '{}')

    def test_launcher_preserves_framing_stderr_and_exit_status(self):
        (self.base / 'Library' / 'Logs').mkdir(parents=True)
        child = self.base / 'synthetic-host.py'
        child.write_text('import sys\nsys.stdout.buffer.write(b"\\x02\\x00\\x00\\x00{}")\nsys.stderr.write("synthetic diagnostic only\\n")\nsys.exit(7)\n')
        launcher = self.base / 'launcher.sh'
        launcher.write_text(installer.launcher_script(sys.executable, child))
        result = subprocess.run(['/bin/sh', str(launcher)], capture_output=True,
                                env={'HOME': str(self.base), 'PATH': '/usr/bin:/bin'}, timeout=5)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, b'\x02\x00\x00\x00{}')
        self.assertEqual(result.stderr, b'')
        log = self.base / 'Library' / 'Logs' / 'QuietBridgeBrowser-native-host.log'
        text = log.read_text()
        self.assertNotIn('host-start', text)
        self.assertIn('synthetic diagnostic only', text)
        self.assertIn('code=7', text)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_launcher_success_is_quiet_unless_debug_is_explicit(self):
        (self.base / 'Library' / 'Logs').mkdir(parents=True)
        child = self.base / 'synthetic-host.py'
        child.write_text('import sys\nsys.stdout.buffer.write(b"\\x02\\x00\\x00\\x00{}")\n')
        launcher = self.base / 'launcher.sh'
        launcher.write_text(installer.launcher_script(sys.executable, child))
        env = {'HOME': str(self.base), 'PATH': '/usr/bin:/bin'}
        log = self.base / 'Library' / 'Logs' / 'QuietBridgeBrowser-native-host.log'
        for debug in (False, True):
            result = subprocess.run(['/bin/sh', str(launcher)], capture_output=True,
                                    env=env | ({'QUIETBRIDGE_NATIVE_HOST_DEBUG': '1'} if debug else {}), timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b'\x02\x00\x00\x00{}')
            self.assertEqual(result.stderr, b'')
            text = log.read_text()
            if debug:
                self.assertIn('launch pid=', text)
                self.assertIn('host-start', text)
                self.assertIn('code=0', text)
            else:
                self.assertEqual(text, '')

    def portable_tree(self):
        root = self.base / 'synthetic portable tree'
        ext = root / 'extensions/chromium'
        ext.mkdir(parents=True)
        bridge = root / 'services/mcp'
        bridge.mkdir(parents=True)
        config = root / 'quietbridge-config'
        config.mkdir()
        (config / 'task-windows.json').write_text(json.dumps(
            {'version': 1, 'tasks': {}, 'activeTaskId': None}))
        shutil.copy2(Path(events.__file__), bridge / 'browser_events.py')
        shutil.copy2(Path(events.__file__).with_name('compatibility.py'), bridge / 'compatibility.py')
        # Subprocess fixtures must not depend on the real installed application.
        with (bridge / 'browser_events.py').open('a') as stream:
            stream.write('\n\ndef application_session(runtime=None):\n    return None\n')
        for name in ('native_host.py', 'install_native_host.py', 'manifest.json'):
            shutil.copy2(EXT / name, ext / name)
        (ext / 'native-host-config.json').write_text(json.dumps({'allowed_origins': [ORIGIN]}))
        launcher = ext / 'native-host-launcher.sh'
        launcher.write_text(installer.launcher_script(installer.pick_interpreter(),
                                                      ext / 'native_host.py'))
        launcher.chmod(0o700)
        return root, ext

    def test_relative_launcher_survives_move_spaces_and_other_working_directory(self):
        root, ext = self.portable_tree()
        generated = (ext / 'native-host-launcher.sh').read_text()
        self.assertNotIn(str(root), generated)
        self.assertNotIn(str(EXT), generated)
        moved = self.base / "moved tree with spaces and 'quote'"
        root.rename(moved)
        ext = moved / 'extensions/chromium'
        source = io.BytesIO()
        host.write_message(source, {'action': 'sync', 'enabled': True, 'permission': True})
        result = subprocess.run([str(ext / 'native-host-launcher.sh'), ORIGIN],
                                input=source.getvalue(), capture_output=True, cwd='/',
                                env={'HOME': str(self.base), 'PATH': '/usr/bin:/bin'}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = io.BytesIO(result.stdout)
        reply = host.read_message(replies)
        self.assertFalse(reply['ok'], reply)
        self.assertEqual(reply['code'], 'APP_NOT_RUNNING')
        self.assertEqual(replies.read(), b'')
        self.assertFalse((moved / 'quietbridge-config' / 'browser-events.json').exists())
        self.assertEqual(list(moved.rglob('__pycache__')), [])

    def test_installer_plans_runtime_dir_outside_documents(self):
        root, ext = self.portable_tree()
        moved = self.base / 'relocated project'
        root.rename(moved)
        ext = moved / 'extensions/chromium'
        result = subprocess.run([sys.executable, '-B', str(ext / 'install_native_host.py')],
                                capture_output=True, cwd='/', timeout=5,
                                env={'HOME': str(self.base), 'PATH': '/usr/bin:/bin'})
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        runtime = self.base / 'Library/Application Support/QuietBridgeBrowser'
        # 注册与运行态都在用户 Library，不在项目或 Documents 下：Chrome 派生的解释器
        # 读不了 TCC 保护的 ~/Documents。
        self.assertEqual(plan['launcherPath'], str(runtime / 'extensions/chromium'
                                                   / 'native-host-launcher.sh'))
        self.assertEqual(plan['manifest']['path'], plan['launcherPath'])
        self.assertEqual(plan['hostPath'], str(runtime / 'extensions/chromium' / 'native_host.py'))
        self.assertEqual(plan['bridgePath'], str(runtime / 'services/mcp' / 'browser_events.py'))
        self.assertEqual(plan['manifestPath'], str(self.base / 'Library/Application Support'
                                                   / 'Google/Chrome/NativeMessagingHosts'
                                                   / 'com.cua.browser_observer.json'))
        self.assertNotIn(str(moved), plan['launcherPath'])
        self.assertTrue(Path(plan['manifest']['path']).is_absolute())
        # origin 由 manifest key 派生，与脚本所在目录无关。
        self.assertEqual(plan['extensionId'], DERIVED_ID)
        self.assertEqual(plan['manifest']['allowed_origins'], [DERIVED_ORIGIN])
        self.assertEqual(plan['mode'], '仅预览，不写文件')
        self.assertFalse(runtime.exists())
        self.assertFalse((moved / 'quietbridge-config' / 'browser-events.json').exists())

    def test_relative_launcher_missing_host_does_not_fall_back_to_old_project(self):
        ext = self.base / 'extension only'
        ext.mkdir()
        launcher = ext / 'launcher.sh'
        launcher.write_text(installer.launcher_script(sys.executable, EXT / 'native_host.py'))
        result = subprocess.run(['/bin/sh', str(launcher), ORIGIN], capture_output=True,
                                cwd='/', timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertIn(b'Native host missing beside launcher', result.stderr)

    def test_missing_companion_bridge_has_explicit_error_without_global_import_fallback(self):
        ext = self.base / 'standalone extension'
        ext.mkdir()
        for name in ('native_host.py', 'install_native_host.py'):
            shutil.copy2(EXT / name, ext / name)
            result = subprocess.run([sys.executable, '-B', str(ext / name), ORIGIN],
                                    capture_output=True, cwd='/', timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b'')
            self.assertIn(b'Missing QuietBridge MCP companion',
                          result.stderr)

    def test_frame_little_endian_partial_and_bounds(self):
        class Fragmented(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 2))
        data = {'action': 'status', 'taskId': 'task'}
        out = io.BytesIO()
        host.write_message(out, data)
        raw = out.getvalue()
        self.assertEqual(struct.unpack('<I', raw[:4])[0], len(raw) - 4)
        self.assertEqual(host.read_message(Fragmented(raw)), data)
        for raw in (b'', b'\x01', struct.pack('<I', 0), struct.pack('<I', 1024 * 1024 + 1),
                    struct.pack('<I', 4) + b'{}', struct.pack('<I', 1) + b'\xff'):
            with self.subTest(raw=raw[:8]), self.assertRaises((ValueError, UnicodeError)):
                host.read_message(io.BytesIO(raw))
        # 允许恰好 1MiB 的请求帧。
        raw = b'{}' + b' ' * (events.MAX_BYTES - 2)
        self.assertEqual(host.read_message(io.BytesIO(struct.pack('<I', len(raw)) + raw)), {})

    def test_serve_single_request_and_illegal_origin(self):
        source = io.BytesIO()
        host.write_message(source, {'action': 'status', 'taskId': 'task'})
        one = source.getvalue()
        for origin, ok in ((ORIGIN, True), ('https://linux.do', False)):
            out = io.BytesIO()
            stream = io.BytesIO(one + one)
            host.serve(stream, out, origin, {'allowed_origins': [ORIGIN]}, **self.paths)
            self.assertEqual(host.read_message(io.BytesIO(out.getvalue()))['ok'], ok)
            self.assertEqual(stream.tell(), len(one) * 2 if ok else 0)
            if ok:
                replies = io.BytesIO(out.getvalue())
                self.assertTrue(host.read_message(replies)['ok'])
                self.assertTrue(host.read_message(replies)['ok'])
        self.assertFalse(self.store.exists())

    def test_framed_legacy_rejection_and_assignment_event_status_chain(self):
        def request(message):
            source, target = io.BytesIO(), io.BytesIO()
            host.write_message(source, message)
            source.seek(0)
            host.serve(source, target, ORIGIN, {'allowed_origins': [ORIGIN]}, **self.paths)
            target.seek(0)
            return host.read_message(target)
        rejected = request({'action': 'bind', 'taskId': 'task', 'confirm': True,
                            'tabId': 7, 'browserWindowId': 2, 'authorizedOrigin': 'https://example.org', 'selector': ''})
        self.assertFalse(rejected['ok'])
        self.assertEqual(rejected['code'], 'LEGACY_UNSUPPORTED')
        self.assertIn('内部 assign', rejected['error'])
        self.assertFalse(self.store.exists())
        token = self.assign()
        assignment = request({'action': 'sync', 'enabled': True, 'permission': True})['assignment']
        self.assertEqual(assignment['token'], token)
        self.assertTrue(request({'action': 'event', 'taskId': 'task', 'token': token,
                                 'event': self.event()})['ok'])
        status = request({'action': 'status', 'taskId': 'task'})['status']
        self.assertIsNone(status['event']['contentReady'])
        self.assertIsNone(status['event']['conditionMatched'])
        self.assertFalse(status['mappingVerified'])
        self.assertNotIn('token', status)

    def test_store_limit_and_invalidated_task(self):
        self.store.write_bytes(b' ' * (events.MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            events.status('task', **self.paths)
        self.tasks.write_text(json.dumps({'version': 1, 'activeTaskId': 'task', 'tasks': {
            'task': {'taskId': 'task', 'state': 'invalidated'}}}))
        with self.assertRaises(ValueError):
            self.assign()

    def sync(self, **extra):
        return self.request({'action': 'sync', 'enabled': True, 'permission': True,
                             'capabilities': ['cdp_input', 'document_binding', 'background_popups']} | extra)

    def prepare_command(self):
        self.prepare_assignment()
        assignment = self.sync()['assignment']
        self.request({'action': 'event', 'taskId': 'task', 'token': assignment['token'], 'event': self.event()})
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'}, **self.paths)
        self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': self.SNAPSHOT})

    def test_render_probe_capability_gate_and_pending_verification(self):
        self.prepare_command()
        args = {'action': 'command', 'taskId': 'task', 'operation': 'render_probe', 'stage': 'inspect'}
        with self.assertRaisesRegex(ValueError, 'EXTENSION_UPGRADE_REQUIRED'):
            events.internal_action(args, **self.paths)
        self.sync(capabilities=['cdp_input', 'document_binding', 'background_popups', 'render_probe'])
        with events.event_store(self.store) as data:
            data['peer']['pendingVerify'] = {'taskId': 'task', 'operation': 'navigate'}
        with self.assertRaisesRegex(ValueError, 'RENDER_PROBE_NOT_READY'):
            events.internal_action(args, **self.paths)
        self.assertEqual(events.read_file(self.store)['peer']['pendingVerify']['operation'], 'navigate')
        self.assertFalse(events.read_file(self.store)['peer'].get('command'))

    def test_render_probe_result_is_document_scoped_and_has_no_input_verification_effect(self):
        self.prepare_command()
        caps = ['cdp_input', 'document_binding', 'background_popups', 'render_probe']
        self.sync(capabilities=caps)
        args = {'action': 'command', 'taskId': 'task', 'operation': 'render_probe', 'stage': 'inspect'}
        queued = events.internal_action(args, **self.paths)
        self.sync(capabilities=caps)
        result = dict(stage='inspect', startedAt=events.time.time(), completedAt=events.time.time(),
            performed=False, attachedBefore=False, attachedAfter=False, pageEnabledBefore=False, pageEnabledAfter=False)
        self.sync(capabilities=caps, commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': result})
        reply = self.poll_command(queued['commandId'])
        self.assertEqual(reply['result'], result)
        self.assertEqual(reply['nextAction'], 'observe')
        self.assertIsNone(events.read_file(self.store)['peer'].get('pendingVerify'))
        queued = events.internal_action(args, **self.paths)
        self.sync(capabilities=caps)
        with events.event_store(self.store) as data:
            data['tasks']['task']['event']['documentId'] = 'new-doc'
        self.sync(capabilities=caps, commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': result})
        self.assertIn('STALE_DOCUMENT', self.poll_command(queued['commandId'])['error'])

    def test_render_probe_strict_reply_and_schema(self):
        r = dict(stage='capture', startedAt=100, completedAt=101, performed=True,
                 attachedBefore=True, attachedAfter=True, pageEnabledBefore=True, pageEnabledAfter=True,
                 captureDataLength=8)
        self.assertEqual(events.validate_render_probe(r, {'stage': 'capture'}), r)
        for bad in [dict(r, data='private'), dict(r, stage='inspect'), dict(r, performed=False),
                    dict(r, startedAt=float('nan')), dict(r, completedAt=float('inf')),
                    dict(r, completedAt=120), dict(r, captureDataLength=True),
                    dict(r, captureDataLength=8.5), dict(r, pageEnabledBefore=False),
                    dict(r, attachedAfter=False)]:
            with self.assertRaises(ValueError):
                events.validate_render_probe(bad, {'stage': 'capture'})
        tool = events.make_tool(lambda: self.tasks)
        self.assertEqual(tool['inputSchema']['properties']['stage']['enum'], list(events.RENDER_PROBE_STAGES))
        with self.assertRaises(ValueError):
            events.command_params({'operation': 'render_probe', 'stage': 'Runtime.evaluate'}, 'render_probe')

    def test_command_ack_loss_accepts_the_same_reply_without_overwriting_or_replaying(self):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'}, **self.paths)
        reply = {'requestId': queued['commandId'], 'status': 'ok', 'result': self.SNAPSHOT}
        self.sync(commandResult=reply)
        self.assertIsNone(self.sync(commandResult=reply)['command'])
        results = events.read_file(self.store)['peer']['commandResults']
        self.assertEqual(sum(r['requestId'] == queued['commandId'] for r in results), 1)

    def poll_command(self, command_id):
        return events.internal_action({'action': 'result', 'taskId': 'task', 'commandId': command_id}, **self.paths)

    def test_expiry_is_terminal_even_without_heartbeats_and_after_new_commands(self):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'}, **self.paths)
        self.sync()
        with events.event_store(self.store) as data:
            data['peer']['command']['expiresAt'] = events.time.time() - 1
            data['peer']['lastSeen'] = events.time.time() - 30
        expired = self.poll_command(queued['commandId'])
        self.assertEqual(expired['status'], 'expired')
        self.assertTrue(expired['mayHaveExecuted'])
        self.sync()
        next_command = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'diagnostics'}, **self.paths)
        ack = self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': self.SNAPSHOT})
        self.assertEqual(ack['commandAck']['status'], 'discarded')
        self.assertEqual(ack['command']['requestId'], next_command['commandId'])
        self.assertEqual(self.poll_command(queued['commandId'])['status'], 'expired')

    def test_expired_input_requires_snapshot_and_is_never_replayed(self):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'navigate',
                                        'url': 'https://example.org/next'}, **self.paths)
        self.sync()
        with events.event_store(self.store) as data:
            data['peer']['command']['expiresAt'] = events.time.time() - 1
        self.assertEqual(self.poll_command(queued['commandId'])['nextAction'], 'snapshot')
        self.assertIsNone(self.sync()['command'])
        shot = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'screenshot'}, **self.paths)
        self.sync(commandResult={'requestId': shot['commandId'], 'status': 'ok', 'result': {
            'mimeType': 'image/jpeg', 'data': 'ZmFrZQ==', 'tabId': 7, 'documentId': 'doc'}})
        with self.assertRaisesRegex(ValueError, '尚未用 snapshot'):
            events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'navigate',
                                    'url': 'https://example.org/next'}, **self.paths)

    def navigation_fixture(self, ready=True, url='https://example.org/next', epoch=2):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'navigate',
                                        'url': 'https://example.org/next'}, **self.paths)
        self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': {
            'performed': True, 'verified': False, 'observed': {'route': 'extension', 'tabId': 7}}})
        self.assertEqual(self.poll_command(queued['commandId'])['nextAction'], 'navigation_observation')
        with events.event_store(self.store) as data:
            data['tasks']['task']['event'].update(documentId='next-doc', navigationEpoch=epoch,
                url=url, phase='content', domReady=ready, loaded=ready, readyState='complete' if ready else 'loading')

    def test_navigation_accepts_current_screenshot_diagnostics_or_read_without_full_snapshot(self):
        for operation in ('screenshot', 'diagnostics', 'read'):
            with self.subTest(operation=operation):
                self.navigation_fixture()
                self.sync(capabilities=['cdp_input', 'document_binding', 'background_popups', 'paged_read'])
                observation = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': operation}, **self.paths)
                outcomes = {
                    'screenshot': {'mimeType': 'image/jpeg', 'data': 'ZmFrZQ==', 'tabId': 7, 'documentId': 'next-doc'},
                    'diagnostics': self.DIAGNOSTICS | {'url': 'https://example.org/next'},
                    'read': {'readId': 'read-next', 'mode': 'content', 'url': 'https://example.org/next', 'title': 'Fixture',
                             'offset': 0, 'nextOffset': None, 'totalItems': 0, 'sourceTruncated': False,
                             'domOnly': True, 'observedAt': 1794210000000, 'items': []}}
                self.sync(commandResult={'requestId': observation['commandId'], 'status': 'ok', 'result': outcomes[operation]})
                self.assertFalse(events.status('task', **self.paths)['commandState']['verificationRequired'])
                # This observation grants no fresh input references.
                with self.assertRaisesRegex(ValueError, 'STALE_SNAPSHOT'):
                    events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'click',
                                            'snapshotId': 'snap-1', 'ref': 1}, **self.paths)
                next_nav = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'navigate',
                                                   'url': 'https://example.org/third'}, **self.paths)
                events.internal_action({'action': 'cancel', 'taskId': 'task', 'commandId': next_nav['commandId']}, **self.paths)

    def test_navigation_lightweight_verification_requires_new_ready_matching_document(self):
        for change in ({'ready': False}, {'url': 'https://example.org/unexpected'}, {'epoch': 1}):
            with self.subTest(change=change):
                self.navigation_fixture(**change)
                if change.get('ready') is False:
                    with mock.patch.object(events, 'NAVIGATION_OBSERVATION_TIMEOUT', 0), self.assertRaises(events.NavigationDocumentPending):
                        events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'screenshot'}, **self.paths)
                    self.assertIsNone(events.read_file(self.store)['peer']['command'])
                    continue
                queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'screenshot'}, **self.paths)
                self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': {
                    'mimeType': 'image/jpeg', 'data': 'ZmFrZQ==', 'tabId': 7, 'documentId': 'next-doc'}})
                self.assertTrue(events.status('task', **self.paths)['commandState']['verificationRequired'])

    def test_redirected_navigation_explicitly_requests_snapshot_without_reassigning_target(self):
        for url in ('https://example.org/next/', 'https://example.org/landing', 'https://other.example/landing'):
            with self.subTest(url=url):
                self.navigation_fixture(url=url)
                binding = events.read_file(self.store)['tasks']['task']['bindingId']
                queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'diagnostics'}, **self.paths)
                self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': self.DIAGNOSTICS | {'url': url}})
                result = self.poll_command(queued['commandId'])
                self.assertEqual(result['nextAction'], 'snapshot')
                self.assertEqual(result['verification']['expectedUrl'], 'https://example.org/next')
                self.assertEqual(result['verification']['observedUrl'], url)
                self.assertTrue(result['verification']['bindingPreserved'])
                status = events.status('task', **self.paths)
                self.assertEqual(status['commandState']['verificationNextAction'], 'snapshot')
                with self.assertRaisesRegex(ValueError, '请获取当前 snapshot'):
                    events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'navigate', 'url': 'https://example.org/third'}, **self.paths)
                reviewed = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'}, **self.paths)
                self.sync(commandResult={'requestId': reviewed['commandId'], 'status': 'ok', 'result': self.SNAPSHOT | {'url': url}})
                self.assertFalse(events.status('task', **self.paths)['commandState']['verificationRequired'])
                self.assertEqual(events.read_file(self.store)['tasks']['task']['bindingId'], binding)

    def test_observation_waits_before_queuing_then_pins_new_document_without_reassignment(self):
        self.navigation_fixture()
        with events.event_store(self.store) as data:
            binding = data['tasks']['task']['bindingId']
            data['tasks']['task']['event'].update(documentId=None, phase='before')
        def commit(_):
            self.assertIsNone(events.read_file(self.store)['peer']['command'])
            with events.event_store(self.store) as data:
                data['tasks']['task']['event'].update(documentId='next-doc', phase='content')
        with mock.patch.object(events.time, 'sleep', side_effect=commit):
            queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'screenshot'}, **self.paths)
        self.assertEqual(queued['target']['documentId'], 'next-doc')
        self.assertEqual(events.read_file(self.store)['tasks']['task']['bindingId'], binding)

    def test_observation_wait_aborts_if_binding_changes(self):
        self.navigation_fixture()
        with events.event_store(self.store) as data:
            data['tasks']['task']['event'].update(documentId=None, phase='before')
        def replace(_):
            with events.event_store(self.store) as data:
                data['tasks']['task']['bindingId'] = 'f' * 32
        with mock.patch.object(events.time, 'sleep', side_effect=replace), self.assertRaisesRegex(ValueError, 'STALE_TARGET'):
            events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'screenshot'}, **self.paths)
        self.assertIsNone(events.read_file(self.store)['peer']['command'])

    def test_waited_observation_rechecks_binding_under_lock_after_native_revalidation(self):
        self.navigation_fixture()
        with events.event_store(self.store) as data:
            data['tasks']['task']['event'].update(documentId=None, phase='before')
        def commit(_):
            with events.event_store(self.store) as data:
                data['tasks']['task']['event'].update(documentId='next-doc', phase='content')
        def change_binding(_):
            with events.event_store(self.store) as data:
                data['tasks']['task']['bindingId'] = 'e' * 32
        with mock.patch.object(events.time, 'sleep', side_effect=commit), self.assertRaisesRegex(ValueError, 'STALE_TARGET'):
            events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'screenshot'},
                                   revalidate=change_binding, **self.paths)
        self.assertIsNone(events.read_file(self.store)['peer']['command'])

    def test_host_reports_rejected_request_separately_without_echoing_payload(self):
        source, target, diagnostic = io.BytesIO(), io.BytesIO(), io.StringIO()
        host.write_message(source, {'action': 'sync', 'secret': 'private-fixture-value'})
        source.seek(0)
        with mock.patch.object(sys, 'stderr', diagnostic):
            host.serve(source, target, ORIGIN, {'allowed_origins': [ORIGIN]}, **self.paths)
        result = host.read_message(io.BytesIO(target.getvalue()))
        self.assertEqual(result['code'], 'BROWSER_REQUEST_REJECTED')
        self.assertNotIn('private-fixture-value', target.getvalue().decode('utf-8', errors='ignore') + diagnostic.getvalue())

    def test_sixty_navigation_screenshot_cycles_reuse_binding_and_keep_store_bounded(self):
        self.prepare_command()
        assignment = self.sync()['assignment']
        event = self.event()
        for index in range(60):
            url = 'https://example.org/fixture-topic/%s' % index
            nav = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'navigate', 'url': url}, **self.paths)
            self.sync()
            before = event | {'documentId': None, 'navigationEpoch': event['navigationEpoch'] + 1,
                'sequence': event['sequence'] + 1, 'url': url, 'phase': 'before', 'loaded': False,
                'domReady': False, 'bodyPresent': False, 'bodyVisible': False,
                'elementCount': 0, 'readyState': None, 'conditionMatched': None}
            self.request({'action': 'event', 'taskId': 'task', 'token': assignment['token'], 'event': before})
            committed = before | {'documentId': 'fixture-doc-%s' % index,
                                  'phase': 'committed', 'sequence': before['sequence'] + 1}
            self.request({'action': 'event', 'taskId': 'task', 'token': assignment['token'], 'event': committed})
            event = committed | {'phase': 'content', 'sequence': committed['sequence'] + 1,
                                 'loaded': True, 'domReady': True, 'bodyPresent': True, 'bodyVisible': True,
                                 'elementCount': 10, 'readyState': 'complete'}
            self.request({'action': 'event', 'taskId': 'task', 'token': assignment['token'], 'event': event})
            self.sync(commandResult={'requestId': nav['commandId'], 'status': 'ok', 'result': {
                'performed': True, 'verified': False, 'observed': {'route': 'extension', 'tabId': 7}}})
            shot = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'screenshot'}, **self.paths)
            self.sync(commandResult={'requestId': shot['commandId'], 'status': 'ok', 'result': {
                'mimeType': 'image/jpeg', 'data': 'ZmFrZQ==', 'tabId': 7, 'documentId': event['documentId']}})
            self.assertEqual(self.poll_command(shot['commandId'])['status'], 'ok')
            self.assertFalse(events.status('task', **self.paths)['commandState']['verificationRequired'])
        stored = events.read_file(self.store)
        self.assertEqual(stored['tasks']['task']['bindingId'], assignment['bindingId'])
        self.assertEqual(stored['tasks']['task']['assignmentVersion'], assignment['assignmentVersion'])
        self.assertLessEqual(len(stored['peer']['commandResults']), events.MAX_RESULTS)
        self.assertLessEqual(len(stored['peer']['commandSeen']), events.MAX_SEEN)
        self.assertLess(self.store.stat().st_size, events.MAX_BYTES)

    def test_old_extension_waits_instead_of_receiving_background_commands(self):
        self.prepare_command()
        self.sync(clientVersion='0.3.0')
        self.request({'action': 'sync', 'enabled': True, 'permission': True, 'clientVersion': '0.2.4'})
        with self.assertRaisesRegex(ValueError, 'BACKGROUND_WAITING'):
            events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'}, **self.paths)

    def test_reference_commands_require_the_current_document_snapshot(self):
        self.prepare_command()
        command = {'action': 'command', 'taskId': 'task', 'operation': 'click', 'ref': 1}
        for extra in ({}, {'snapshotId': 'older-snapshot'}):
            with self.assertRaisesRegex(ValueError, 'STALE_SNAPSHOT'):
                events.internal_action(command | extra, **self.paths)
        with events.event_store(self.store) as data:
            data['tasks']['task']['event']['documentId'] = 'replacement-document'
        with self.assertRaisesRegex(ValueError, 'STALE_SNAPSHOT'):
            events.internal_action(command | {'snapshotId': 'snap-1'}, **self.paths)

    def test_children_are_owned_and_selecting_them_changes_only_the_logical_target(self):
        self.prepare_command()
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'click',
                                         'ref': 1, 'snapshotId': 'snap-1'}, **self.paths)
        self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok',
                                 'result': {'performed': True, 'verified': False, 'observed': {'route': 'cdp'},
                                            'children': [{'tabId': 9, 'browserWindowId': 2, 'url': 'https://example.org/child'}]}})
        tabs = events.internal_action({'action': 'tabs', 'taskId': 'task'}, **self.paths)
        self.assertEqual(tabs['logicalActiveTabId'], 7)
        self.assertEqual([t['tabId'] for t in tabs['tabs']], [7, 9])
        with self.assertRaisesRegex(ValueError, '只能选择'):
            events.internal_action({'action': 'select_tab', 'taskId': 'task', 'tabId': 123}, **self.paths)
        selected = events.internal_action({'action': 'select_tab', 'taskId': 'task', 'tabId': 9}, **self.paths)
        self.assertTrue(selected['background'])
        assignment = self.sync()['assignment']
        self.assertEqual(assignment['rootTabId'], 7)
        self.assertEqual(assignment['browserWindowId'], 2)
        self.assertIsNone(assignment['event'])
        with self.assertRaisesRegex(ValueError, '等待原标签文档'):
            events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'}, **self.paths)

    def test_diagnostics_cannot_clear_an_unverified_action(self):
        self.prepare_command()
        click = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'click',
                                        'ref': 1, 'snapshotId': 'snap-1'}, **self.paths)
        self.sync(commandResult={'requestId': click['commandId'], 'status': 'ok',
                                 'result': {'performed': True, 'verified': False, 'observed': {}}})
        diagnostic = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'diagnostics'}, **self.paths)
        self.sync(commandResult={'requestId': diagnostic['commandId'], 'status': 'ok', 'result': self.DIAGNOSTICS})
        with self.assertRaisesRegex(ValueError, '尚未用 snapshot'):
            events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'keypress', 'key': 'Enter'}, **self.paths)

    def prepare_assignment(self):
        data = events.read_file(self.tasks)
        data['tasks']['task'].update(state='bound', windowId=900, pid=88,
            processIdentity={'pid': 88, 'startedSeconds': '10', 'startedMicroseconds': '1'}, sessionId='session')
        self.tasks.write_text(json.dumps(data))
        self.sync()
        events.internal_action({'action': 'candidates'}, **self.paths)
        self.sync(candidates=[{'tabId': 7, 'browserWindowId': 2, 'url': 'https://example.org/path'}])
        return events.internal_action({'action': 'assign', 'taskId': 'task', 'tabId': 7,
                                       'browserWindowId': 2}, **self.paths)

    def test_internal_assignment_heartbeat_nonce_cross_site_and_revoke(self):
        self.assertEqual(self.sync()['taskAssignment'], 'needs_target_selection')
        self.assertIsNone(self.sync()['assignment'])
        original = self.tasks.read_bytes()
        result = self.prepare_assignment()
        self.assertFalse(result['mappingVerified'])
        assignment = self.sync()['assignment']
        self.assertEqual(assignment['taskId'], 'task')
        self.assertEqual(assignment['browserWindowId'], 2)
        self.assertNotEqual(assignment['browserWindowId'], assignment['targetIdentity']['windowId'])
        event = self.event() | {'conditionMatched': None, 'url': 'http://other.example:8080/path'}
        message = {'action': 'event', 'taskId': 'task', 'token': assignment['token'], 'event': event}
        self.assertTrue(self.request(message)['ok'])
        for update in ({'taskId': 'other'}, {'token': '0' * 64},
                       {'event': event | {'bindingId': '0' * 32, 'sequence': 2}}):
            with self.assertRaises(ValueError):
                self.request(message | update)
        events.internal_action({'action': 'assign', 'taskId': 'task', 'tabId': 7,
                                'browserWindowId': 2}, **self.paths)
        newer = self.sync()['assignment']
        self.assertGreater(newer['assignmentVersion'], assignment['assignmentVersion'])
        with self.assertRaises(ValueError):
            self.request(message)
        self.assertEqual(self.sync()['assignment']['token'], newer['token'])
        self.sync(permission=False, enabled=False)
        self.assertIsNone(self.sync()['assignment'])
        self.assertFalse(events.status('task', **self.paths)['paired'])
        data = events.read_file(self.store)
        self.assertEqual(data['peer']['candidates'], [])
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.tasks.read_bytes(), original)

    def test_mcp_assign_validates_native_task_before_dispatch(self):
        self.prepare_assignment()
        args = {'action': 'assign', 'taskId': 'task', 'tabId': 7, 'browserWindowId': 2}
        original = events.internal_action
        with mock.patch.object(server, 'TASK_FILE', str(self.tasks)), \
                mock.patch.object(server, 'task_status', side_effect=lambda record: dict(record)), \
                mock.patch.object(server, 'call_helper', return_value={}) as helper, \
                mock.patch.object(events, 'internal_action', wraps=events.internal_action) as dispatch:
            # 隔离 event store，但经过实际注册工具和 server 原生任务核验回调。
            dispatch.side_effect = lambda a, task_file, event_file=None, **options: original(a, task_file, event_file or self.store, **options)
            response = server.call_tool('quietbridge_browser_events', args)
            self.assertFalse(response.get('isError'), response)
            self.assertTrue(json.loads(response['content'][0]['text'])['assigned'])
            helper.assert_called_once_with('preview', {'action': 'release'})
        with mock.patch.object(server, 'TASK_FILE', str(self.tasks)), \
                mock.patch.object(server, 'task_status', return_value={'state': 'requires_revalidation'}), \
                mock.patch.object(events, 'internal_action') as dispatch:
            response = server.call_tool('quietbridge_browser_events', args)
            self.assertTrue(response.get('isError'))
            dispatch.assert_not_called()

    def test_assignment_needs_evidence_bound_active_target_and_recent_peer(self):
        self.sync()
        args = {'action': 'assign', 'taskId': 'task', 'tabId': 7, 'browserWindowId': 2}
        with self.assertRaises(ValueError):
            events.internal_action(args, **self.paths)
        self.prepare_assignment()
        for change in ({'tabId': 99}, {'browserWindowId': 900}, {'taskId': 'other'}):
            with self.assertRaises(ValueError):
                events.internal_action(args | change, **self.paths)
        with mock.patch.object(events.time, 'time', return_value=events.time.time() + 100):
            with self.assertRaises(ValueError):
                events.internal_action(args, **self.paths)
            self.assertEqual(events.status('task', **self.paths)['nativeConnection'], 'host_unavailable')
        data = events.read_file(self.tasks)
        data['activeTaskId'] = 'other'
        self.tasks.write_text(json.dumps(data))
        self.assertIsNone(self.sync()['assignment'])

    def stale_assignment_candidate(self):
        self.prepare_assignment()
        with events.event_store(self.store) as data:
            data['tasks'] = {}
            data['peer']['candidatesAt'] -= 35
        return {'action': 'assign', 'taskId': 'task', 'tabId': 7, 'browserWindowId': 2}

    def test_assign_refreshes_expired_evidence_without_requiring_another_model_turn(self):
        args = self.stale_assignment_candidate()
        candidate = {'tabId': 7, 'browserWindowId': 2, 'url': 'https://example.org/path'}
        def extension_sync(_):
            self.assertTrue(events.read_file(self.store)['peer']['requestCandidates'])
            self.sync(candidates=[candidate])
        with mock.patch.object(events.time, 'sleep', side_effect=extension_sync) as wait, \
                mock.patch.object(events.time, 'monotonic', return_value=0):
            result = events.internal_action(args, **self.paths)
        self.assertTrue(result['assigned'])
        self.assertTrue(result['candidateRefreshed'])
        self.assertLess(result['candidateAgeMs'], 1000)
        wait.assert_called_once()

    def test_assign_does_not_wait_when_candidate_evidence_is_fresh(self):
        self.prepare_assignment()
        with mock.patch.object(events.time, 'sleep') as wait:
            result = events.internal_action({'action': 'assign', 'taskId': 'task',
                                            'tabId': 7, 'browserWindowId': 2}, **self.paths)
        self.assertTrue(result['assigned'])
        self.assertFalse(result['candidateRefreshed'])
        wait.assert_not_called()

    def test_candidate_refresh_rejects_closed_moved_or_navigated_target(self):
        for candidates in ([], [{'tabId': 7, 'browserWindowId': 3, 'url': 'https://example.org/path'}],
                           [{'tabId': 7, 'browserWindowId': 2, 'url': 'https://other.example/path'}]):
            with self.subTest(candidates=candidates):
                args = self.stale_assignment_candidate()
                with mock.patch.object(events.time, 'sleep', side_effect=lambda _: self.sync(candidates=candidates)), \
                        mock.patch.object(events.time, 'monotonic', return_value=0), self.assertRaises(ValueError):
                    events.internal_action(args, **self.paths)
                self.assertEqual(events.read_file(self.store)['tasks'], {})

    def test_candidate_refresh_times_out_without_assigning_or_replaying_actions(self):
        args = self.stale_assignment_candidate()
        with mock.patch.object(events.time, 'monotonic', side_effect=[0, 4]), \
                mock.patch.object(events.time, 'sleep') as wait, \
                self.assertRaisesRegex(ValueError, 'CANDIDATE_REFRESH_TIMEOUT'):
            events.internal_action(args, **self.paths)
        wait.assert_not_called()
        self.assertEqual(events.read_file(self.store)['tasks'], {})

    def test_candidate_refresh_rechecks_permission_and_native_binding(self):
        candidate = {'tabId': 7, 'browserWindowId': 2, 'url': 'https://example.org/path'}
        for change in ('permission', 'target', 'session'):
            with self.subTest(change=change):
                args = self.stale_assignment_candidate()
                def changed(_):
                    self.sync(candidates=[candidate])
                    if change == 'target':
                        data = events.read_file(self.tasks)
                        data['tasks']['task']['windowId'] = 901
                        self.tasks.write_text(json.dumps(data))
                    else:
                        with events.event_store(self.store) as data:
                            data['peer'].update({'permission': False} if change == 'permission' else
                                                {'applicationSession': 'another-session'})
                with mock.patch.object(events.time, 'sleep', side_effect=changed), \
                        mock.patch.object(events.time, 'monotonic', return_value=0), self.assertRaises(ValueError):
                    events.internal_action(args, **self.paths)
                self.assertEqual(events.read_file(self.store)['tasks'], {})

    def test_native_revalidation_failure_after_candidate_refresh_prevents_assignment(self):
        args = self.stale_assignment_candidate()
        candidate = {'tabId': 7, 'browserWindowId': 2, 'url': 'https://example.org/path'}
        validate = mock.Mock(side_effect=ValueError('native target no longer valid'))
        with mock.patch.object(events.time, 'sleep', side_effect=lambda _: self.sync(candidates=[candidate])), \
                mock.patch.object(events.time, 'monotonic', return_value=0), \
                self.assertRaisesRegex(ValueError, 'native target no longer valid'):
            events.internal_action(args, revalidate=validate, **self.paths)
        validate.assert_called_once_with('task')
        self.assertEqual(events.read_file(self.store)['tasks'], {})

    def test_default_no_task_and_native_multi_frame_truncation(self):
        self.tasks.unlink()
        source, target = io.BytesIO(), io.BytesIO()
        for _ in range(2):
            host.write_message(source, {'action': 'sync', 'enabled': False, 'permission': False})
        source.write(b'\x05\x00')
        source.seek(0)
        host.serve(source, target, ORIGIN, {'allowed_origins': [ORIGIN]}, **self.paths)
        target.seek(0)
        for _ in range(2):
            reply = host.read_message(target)
            self.assertTrue(reply['ok'])
            self.assertIsNone(reply['assignment'])
        self.assertFalse(host.read_message(target)['ok'])
        self.assertFalse(self.tasks.exists())
        self.assertEqual(self.sync()['taskAssignment'], 'no_assignment')

    def test_candidates_are_on_demand_metadata_only_and_origin_checked(self):
        self.sync()
        candidate = {'tabId': 7, 'browserWindowId': 2, 'url': 'https://example.org/path'}
        with self.assertRaises(ValueError):
            self.sync(candidates=[candidate])
        events.internal_action({'action': 'candidates'}, **self.paths)
        for change in ({'body': 'private'}, {'url': 'https://example.org/path?secret=x'},
                       {'url': 'https://u:p@example.org/path'}, {'url': 'chrome://settings'}):
            with self.assertRaises(ValueError):
                self.sync(candidates=[candidate | change])
        with self.assertRaises(ValueError):
            self.request({'action': 'sync', 'enabled': True, 'permission': True}, 'https://example.org')
        self.assertTrue(self.sync(candidates=[candidate])['ok'])

    def test_installer_dryrun_plan_has_real_origin_without_writes(self):
        result = installer.plan(home=self.base)
        self.assertEqual(result['extensionId'], DERIVED_ID)
        self.assertEqual(result['manifest']['allowed_origins'], [DERIVED_ORIGIN])
        self.assertFalse(Path(result['manifestPath']).exists())
        # 手填 ID 必须与 key 派生值一致，否则拒绝。
        self.assertEqual(installer.plan(DERIVED_ID, home=self.base)['extensionId'], DERIVED_ID)
        for value in ('', 'a' * 31, 'z' * 32, 'a' * 32 + '/x', 'a' * 32):
            with self.assertRaises(ValueError):
                installer.plan(value)

    def test_manifest_key_derives_stable_id_and_missing_key_is_explicit(self):
        self.assertRegex(DERIVED_ID, r'^[a-p]{32}$')
        copy = self.base / 'other name'
        copy.mkdir()
        shutil.copy2(EXT / 'manifest.json', copy / 'manifest.json')
        # 同一 key 换目录仍是同一 ID。
        self.assertEqual(installer.extension_id_from_manifest(copy / 'manifest.json'), DERIVED_ID)
        (copy / 'manifest.json').write_text(json.dumps({'manifest_version': 3, 'name': 'x'}))
        with self.assertRaises(ValueError):
            installer.extension_id_from_manifest(copy / 'manifest.json')
        (copy / 'manifest.json').write_text(json.dumps({'key': 'not base64!'}))
        with self.assertRaises(ValueError):
            installer.extension_id_from_manifest(copy / 'manifest.json')

    def test_config_dir_prefers_env_then_runtime_then_project(self):
        with mock.patch.object(events, 'RUNTIME', self.base / 'rt'), \
                mock.patch.object(events, 'ROOT', self.base / 'proj'), \
                mock.patch.dict(os.environ, {'QUIETBRIDGE_CONFIG_DIR': str(self.base / 'env')}):
            self.assertEqual(events.config_dir(), self.base / 'env')
            os.environ.pop('QUIETBRIDGE_CONFIG_DIR')
            self.assertEqual(events.config_dir(), self.base / 'proj' / 'quietbridge-config')
            (self.base / 'rt' / 'quietbridge-config').mkdir(parents=True)
            self.assertEqual(events.config_dir(), self.base / 'rt' / 'quietbridge-config')

    def test_launcher_uses_system_interpreter_not_session_python(self):
        path = installer.pick_interpreter()
        self.assertTrue(path.is_absolute())
        self.assertTrue(os.access(path, os.X_OK))
        generated = installer.launcher_script(path, EXT / 'native_host.py')
        self.assertIn(str(path) + ' -B "$host"', generated)
        # 解释器不能是工具会话自带的临时 Python，也不能写死项目目录。
        self.assertNotIn('.workbuddy/binaries', generated)
        self.assertNotIn(str(EXT), generated)

    def test_installer_deploys_self_contained_runtime_and_migrates_state(self):
        root, ext = self.portable_tree()
        run = lambda: subprocess.run([sys.executable, '-B', str(ext / 'install_native_host.py'),
                                      '--install', '--update'], capture_output=True, cwd='/',
                                     timeout=10, env={'HOME': str(self.base), 'PATH': '/usr/bin:/bin'})
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome['mode'], '已安装')
        runtime = self.base / 'Library/Application Support/QuietBridgeBrowser'
        host_dir = runtime / 'extensions/chromium'
        # 运行态是自包含副本：host 与 bridge 都不再指向项目目录。
        self.assertEqual((host_dir / 'native_host.py').read_bytes(),
                         (ext / 'native_host.py').read_bytes())
        self.assertEqual((runtime / 'services/mcp' / 'browser_events.py').read_bytes(),
                         (ext.parents[1] / 'services/mcp' / 'browser_events.py').read_bytes())
        self.assertEqual(json.loads((host_dir / 'native-host-config.json').read_text()),
                         {'allowed_origins': [DERIVED_ORIGIN]})
        # 首次部署把项目内的记录迁到运行态。
        self.assertIn('task-windows.json', outcome['migratedState'])
        state = json.loads((runtime / 'quietbridge-config' / 'task-windows.json').read_text())
        self.assertEqual(state['activeTaskId'], None)
        registration = json.loads((self.base / 'Library/Application Support/Google/Chrome'
                                  / 'NativeMessagingHosts/com.cua.browser_observer.json').read_text())
        self.assertEqual(registration['path'], str(host_dir / 'native-host-launcher.sh'))
        self.assertEqual(registration['allowed_origins'], [DERIVED_ORIGIN])
        self.assertEqual((host_dir / 'native-host-launcher.sh').stat().st_mode & 0o777, 0o700)
        # 部署出来的启动器可以用最小环境真实握手，证明副本自洽。
        source = io.BytesIO()
        host.write_message(source, {'action': 'sync', 'enabled': True, 'permission': True})
        live = subprocess.run([str(host_dir / 'native-host-launcher.sh'), DERIVED_ORIGIN],
                              input=source.getvalue(), capture_output=True, cwd='/', timeout=10,
                              env={'HOME': str(self.base), 'PATH': '/usr/bin:/bin'})
        self.assertEqual(live.returncode, 0, live.stderr)
        reply = host.read_message(io.BytesIO(live.stdout))
        self.assertFalse(reply['ok'], reply)
        self.assertEqual(reply['code'], 'APP_NOT_RUNNING')
        # 重复部署幂等。
        again = json.loads(run().stdout)
        self.assertEqual(again['mode'], '已更新')
        self.assertFalse(again['changed'], again)
        self.assertEqual(again['migratedState'], [])

    def test_installer_refuses_foreign_registration(self):
        root, ext = self.portable_tree()
        host_dir = self.base / 'Library/Application Support/Google/Chrome/NativeMessagingHosts'
        host_dir.mkdir(parents=True)
        (host_dir / 'com.cua.browser_observer.json').write_text(json.dumps(
            {'name': 'com.example.other', 'allowed_origins': ['chrome-extension://' + 'c' * 32 + '/']}))
        result = subprocess.run([sys.executable, '-B', str(ext / 'install_native_host.py'),
                                 '--install', '--update'], capture_output=True, cwd='/', timeout=10,
                                env={'HOME': str(self.base), 'PATH': '/usr/bin:/bin'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('拒绝覆盖', result.stderr.decode('utf-8'))
        self.assertEqual(json.loads((host_dir / 'com.cua.browser_observer.json').read_text())['name'],
                         'com.example.other')

    def test_mcp_observation_only_no_helper_or_wait_ready_changes(self):
        token = self.assign()
        self.request({'action': 'event', 'taskId': 'task', 'token': token, 'event': self.event()})
        original_status = events.status
        def isolated_status(task_id, task_file=events.TASK_FILE, event_file=events.EVENT_FILE):
            return original_status(task_id, self.tasks, self.store)
        with mock.patch.object(server, 'TASK_FILE', str(self.tasks)), \
                mock.patch.object(events, 'status', side_effect=isolated_status), \
                mock.patch.object(server, 'HELPER') as helper:
            result = server.call_tool('quietbridge_browser_events', {'action': 'status', 'taskId': 'task'})
            self.assertFalse(result.get('isError'), result)
            payload = json.loads(result['content'][0]['text'])
            self.assertFalse(payload['mappingVerified'])
            self.assertFalse(payload['observationOnly'])
            self.assertFalse(payload['executionPolicy']['fallbackAllowed'])
            self.assertIsNone(payload['event']['contentReady'])
            self.assertIsNone(payload['event']['conditionMatched'])
            helper.request.assert_not_called()
        self.assertNotIn('quietbridge_browser_events', server.TASK_TARGET_TOOLS)
        self.assertFalse(server.TOOL_MAP['quietbridge_browser_events']['readOnly'])


if __name__ == '__main__':
    unittest.main()
