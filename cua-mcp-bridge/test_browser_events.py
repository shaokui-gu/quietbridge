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

# 运行态默认目录是真实用户的 ~/Library/Application Support；测试必须隔离，
# 否则会读写本机的任务记录。
os.environ['CUA_CONFIG_DIR'] = tempfile.mkdtemp(prefix='cua-test-config-')

import browser_events as events
import server

EXT = Path(__file__).resolve().parent.parent / 'browser-observer-extension'


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
    def setUp(self):
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

    def test_create_target_works_before_binding(self):
        # 建窗必须发生在绑定之前，因此不能用 sessionId 作为前置条件。
        data = events.read_file(self.tasks)
        data['tasks']['task'].update(state='awaiting_window')
        for key in ('windowId', 'pid', 'processIdentity', 'sessionId'):
            data['tasks']['task'].pop(key, None)
        self.tasks.write_text(json.dumps(data))
        self.sync()
        queued = events.internal_action({'action': 'create_target', 'taskId': 'task',
                                         'url': 'https://example.org/'}, **self.paths)
        self.assertTrue(queued['requested'])
        self.assertEqual(self.sync()['createRequest']['url'], 'https://example.org/')

    def test_create_target_is_single_flight_and_bound_to_active_task(self):
        self.sync()
        first = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        self.assertTrue(first['requested'])
        self.assertRegex(first['requestId'], '^[0-9a-f]{32}$')
        self.assertEqual(self.sync()['createRequest']['requestId'], first['requestId'])
        with self.assertRaises(ValueError):
            events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        with self.assertRaises(ValueError):
            events.internal_action({'action': 'create_target', 'taskId': 'other'}, **self.paths)

    def test_created_result_needs_the_pending_request_and_clears_it(self):
        self.sync()
        request = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        created = {'requestId': request['requestId'], 'browserWindowId': 5, 'tabId': 9}
        with self.assertRaises(ValueError):
            self.sync(created=created | {'requestId': '0' * 32})
        self.assertTrue(self.sync(created=created)['ok'])
        self.assertIsNone(self.sync()['createRequest'])
        with self.assertRaises(ValueError):
            self.sync(created=created)

    def test_assign_distinguishes_created_and_selected_windows(self):
        self.sync()
        request = events.internal_action({'action': 'create_target', 'taskId': 'task'}, **self.paths)
        self.sync(created={'requestId': request['requestId'], 'browserWindowId': 2, 'tabId': 7})
        events.internal_action({'action': 'candidates'}, **self.paths)
        self.sync(candidates=[{'tabId': 7, 'browserWindowId': 2, 'url': 'https://example.org/path'}])
        result = events.internal_action({'action': 'assign', 'taskId': 'task', 'tabId': 7,
                                         'browserWindowId': 2}, **self.paths)
        self.assertEqual(result['targetEvidence']['mode'], 'created')
        self.assertEqual(result['targetEvidence']['createdRequestId'], request['requestId'])
        self.assertEqual(self.sync()['assignment']['targetEvidence']['mode'], 'created')

    def test_selected_window_is_not_reported_as_created(self):
        self.prepare_assignment()
        self.assertEqual(self.sync()['assignment']['targetEvidence']['mode'], 'selected')
        self.assertIsNone(self.sync()['assignment']['targetEvidence']['createdRequestId'])

    def test_created_window_belongs_only_to_its_active_task(self):
        # 单活动任务模型：切换活动任务后，旧建窗证据失效，新任务可按普通候选选择。
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

    SNAPSHOT = {'url': 'https://example.org/path', 'readyState': 'complete', 'elementCount': 12,
                'elements': [{'ref': 1, 'tag': 'a', 'role': 'link', 'name': 'Docs',
                              'disabled': False, 'x': 0, 'y': 0, 'width': 10, 'height': 10}]}

    def test_command_requires_assignment_and_is_single_flight(self):
        self.sync()
        with self.assertRaises(ValueError):
            events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'},
                                   **self.paths)
        self.prepare_assignment()
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
        self.prepare_assignment()
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
        self.prepare_assignment()
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
        self.prepare_assignment()
        queued = events.internal_action({'action': 'command', 'taskId': 'task', 'operation': 'snapshot'},
                                        **self.paths)
        broken = self.SNAPSHOT | {'elements': [self.SNAPSHOT['elements'][0] | {'ref': 0}]}
        with self.assertRaises(ValueError):
            self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': broken})
        extra = self.SNAPSHOT | {'elements': [self.SNAPSHOT['elements'][0] | {'href': 'x'}]}
        with self.assertRaises(ValueError):
            self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok', 'result': extra})
        self.assertEqual(self.sync()['command']['requestId'], queued['commandId'])

    def test_action_commands_validate_their_own_parameters(self):
        self.prepare_assignment()
        submit = lambda **extra: events.internal_action(  # noqa: E731
            dict({'action': 'command', 'taskId': 'task'}, **extra), **self.paths)
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
        self.assertTrue(self.sync()['command']['params'] == {'ref': 1, 'value': 'hello'})

    def test_action_result_reports_performed_and_verified_separately(self):
        self.prepare_assignment()
        queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                         'operation': 'click', 'ref': 3}, **self.paths)
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

    def test_malformed_action_result_is_rejected(self):
        self.prepare_assignment()
        queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                         'operation': 'scroll', 'deltaY': -3}, **self.paths)
        with self.assertRaises(ValueError):
            self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok',
                                     'result': {'performed': True, 'verified': True,
                                                'observed': {'before': 0, 'after': 80, 'extra': 1,
                                                             'note': 'x' * 300}}})
        with self.assertRaises(ValueError):
            self.sync(commandResult={'requestId': queued['commandId'], 'status': 'ok',
                                     'result': {'performed': 'yes', 'verified': True, 'observed': {}}})

    def test_performed_action_requires_a_snapshot_before_the_next_action(self):
        self.prepare_assignment()
        poll = lambda cid: events.internal_action(  # noqa: E731
            {'action': 'result', 'taskId': 'task', 'commandId': cid}, **self.paths)
        click = events.internal_action({'action': 'command', 'taskId': 'task',
                                        'operation': 'click', 'ref': 3}, **self.paths)
        self.sync(commandResult={'requestId': click['commandId'], 'status': 'ok',
                                 'result': {'performed': True, 'verified': False,
                                            'observed': {'tag': 'a'}}})
        self.assertEqual(poll(click['commandId'])['nextAction'], 'snapshot')
        with self.assertRaises(ValueError):
            events.internal_action({'action': 'command', 'taskId': 'task',
                                    'operation': 'click', 'ref': 3}, **self.paths)
        shot = events.internal_action({'action': 'command', 'taskId': 'task',
                                       'operation': 'snapshot'}, **self.paths)
        self.sync(commandResult={'requestId': shot['commandId'], 'status': 'ok',
                                 'result': self.SNAPSHOT})
        self.assertEqual(poll(shot['commandId'])['nextAction'], 'observe')
        events.internal_action({'action': 'command', 'taskId': 'task',
                                'operation': 'click', 'ref': 3}, **self.paths)

    def test_failed_action_does_not_demand_verification(self):
        self.prepare_assignment()
        click = events.internal_action({'action': 'command', 'taskId': 'task',
                                        'operation': 'click', 'ref': 99}, **self.paths)
        self.sync(commandResult={'requestId': click['commandId'], 'status': 'ok',
                                 'result': {'performed': False, 'verified': False,
                                            'observed': {'reason': 'stale_ref'}}})
        reply = events.internal_action({'action': 'result', 'taskId': 'task',
                                        'commandId': click['commandId']}, **self.paths)
        self.assertEqual(reply['nextAction'], 'done')
        events.internal_action({'action': 'command', 'taskId': 'task',
                                'operation': 'click', 'ref': 3}, **self.paths)

    def test_error_result_tells_the_agent_to_stop(self):
        self.prepare_assignment()
        shot = events.internal_action({'action': 'command', 'taskId': 'task',
                                       'operation': 'snapshot'}, **self.paths)
        self.sync(commandResult={'requestId': shot['commandId'], 'status': 'error',
                                 'error': '缺少已核验的文档身份'})
        reply = events.internal_action({'action': 'result', 'taskId': 'task',
                                        'commandId': shot['commandId']}, **self.paths)
        self.assertEqual(reply['status'], 'error')
        self.assertEqual(reply['nextAction'], 'stop')

    def test_navigate_requires_a_plain_http_url(self):
        self.prepare_assignment()
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

    def test_cancel_before_delivery_reports_it_may_not_have_executed(self):
        self.prepare_assignment()
        queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                         'operation': 'click', 'ref': 3}, **self.paths)
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
        self.prepare_assignment()
        queued = events.internal_action({'action': 'command', 'taskId': 'task',
                                         'operation': 'click', 'ref': 3}, **self.paths)
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
        self.prepare_assignment()
        self.assertFalse(self.request({'action': 'status', 'taskId': 'task'})['status']['commandState']['verificationRequired'])
        click = events.internal_action({'action': 'command', 'taskId': 'task',
                                        'operation': 'click', 'ref': 3}, **self.paths)
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
        self.prepare_assignment()
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
        with self.assertRaises(ValueError):
            self.sync(commandResult={'requestId': again['commandId'], 'status': 'ok',
                                     'result': self.DIAGNOSTICS | {'cookie': 'x'}})

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
        self.prepare_assignment()
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
        self.prepare_assignment()
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
        self.prepare_assignment()
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
        self.prepare_assignment()
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
        log = self.base / 'Library' / 'Logs' / 'WorkbuddyExtension-native-host.log'
        text = log.read_text()
        self.assertIn('host-start', text)
        self.assertIn('synthetic diagnostic only', text)
        self.assertIn('code=7', text)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def portable_tree(self):
        root = self.base / 'synthetic portable tree'
        ext = root / 'browser-observer-extension'
        ext.mkdir(parents=True)
        bridge = root / 'cua-mcp-bridge'
        bridge.mkdir()
        config = root / 'cua-helper-config'
        config.mkdir()
        (config / 'task-windows.json').write_text(json.dumps(
            {'version': 1, 'tasks': {}, 'activeTaskId': None}))
        shutil.copy2(Path(events.__file__), bridge / 'browser_events.py')
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
        ext = moved / ext.name
        source = io.BytesIO()
        host.write_message(source, {'action': 'sync', 'enabled': True, 'permission': True})
        result = subprocess.run([str(ext / 'native-host-launcher.sh'), ORIGIN],
                                input=source.getvalue(), capture_output=True, cwd='/',
                                env={'HOME': str(self.base), 'PATH': '/usr/bin:/bin'}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = io.BytesIO(result.stdout)
        reply = host.read_message(replies)
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['protocolVersion'], 2)
        self.assertEqual(reply['taskAssignment'], 'no_assignment')
        self.assertEqual(replies.read(), b'')
        self.assertTrue((moved / 'cua-helper-config' / 'browser-events.json').exists())
        self.assertEqual(list(moved.rglob('__pycache__')), [])

    def test_installer_plans_runtime_dir_outside_documents(self):
        root, ext = self.portable_tree()
        moved = self.base / 'relocated project'
        root.rename(moved)
        ext = moved / ext.name
        result = subprocess.run([sys.executable, '-B', str(ext / 'install_native_host.py')],
                                capture_output=True, cwd='/', timeout=5,
                                env={'HOME': str(self.base), 'PATH': '/usr/bin:/bin'})
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        runtime = self.base / 'Library/Application Support/WorkbuddyExtension'
        # 注册与运行态都在用户 Library，不在项目或 Documents 下：Chrome 派生的解释器
        # 读不了 TCC 保护的 ~/Documents。
        self.assertEqual(plan['launcherPath'], str(runtime / 'browser-observer-extension'
                                                   / 'native-host-launcher.sh'))
        self.assertEqual(plan['manifest']['path'], plan['launcherPath'])
        self.assertEqual(plan['hostPath'], str(runtime / 'browser-observer-extension' / 'native_host.py'))
        self.assertEqual(plan['bridgePath'], str(runtime / 'cua-mcp-bridge' / 'browser_events.py'))
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
        self.assertFalse((moved / 'cua-helper-config' / 'browser-events.json').exists())

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
            self.assertIn(b'Missing companion component: ../cua-mcp-bridge/browser_events.py',
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
        return self.request({'action': 'sync', 'enabled': True, 'permission': True} | extra)

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
                mock.patch.object(events, 'internal_action', wraps=events.internal_action) as dispatch:
            # 隔离 event store，但经过实际注册工具和 server 原生任务核验回调。
            dispatch.side_effect = lambda a, task_file: original(a, task_file, self.store)
            response = server.call_tool('cua_browser_events', args)
            self.assertFalse(response.get('isError'), response)
            self.assertTrue(json.loads(response['content'][0]['text'])['assigned'])
        with mock.patch.object(server, 'TASK_FILE', str(self.tasks)), \
                mock.patch.object(server, 'task_status', return_value={'state': 'requires_revalidation'}), \
                mock.patch.object(events, 'internal_action') as dispatch:
            response = server.call_tool('cua_browser_events', args)
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
                mock.patch.dict(os.environ, {'CUA_CONFIG_DIR': str(self.base / 'env')}):
            self.assertEqual(events.config_dir(), self.base / 'env')
            os.environ.pop('CUA_CONFIG_DIR')
            self.assertEqual(events.config_dir(), self.base / 'proj' / 'cua-helper-config')
            (self.base / 'rt' / 'cua-helper-config').mkdir(parents=True)
            self.assertEqual(events.config_dir(), self.base / 'rt' / 'cua-helper-config')

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
        runtime = self.base / 'Library/Application Support/WorkbuddyExtension'
        host_dir = runtime / 'browser-observer-extension'
        # 运行态是自包含副本：host 与 bridge 都不再指向项目目录。
        self.assertEqual((host_dir / 'native_host.py').read_bytes(),
                         (ext / 'native_host.py').read_bytes())
        self.assertEqual((runtime / 'cua-mcp-bridge' / 'browser_events.py').read_bytes(),
                         (ext.parent / 'cua-mcp-bridge' / 'browser_events.py').read_bytes())
        self.assertEqual(json.loads((host_dir / 'native-host-config.json').read_text()),
                         {'allowed_origins': [DERIVED_ORIGIN]})
        # 首次部署把项目内的记录迁到运行态。
        self.assertIn('task-windows.json', outcome['migratedState'])
        state = json.loads((runtime / 'cua-helper-config' / 'task-windows.json').read_text())
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
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['taskAssignment'], 'no_assignment')
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
            result = server.call_tool('cua_browser_events', {'action': 'status', 'taskId': 'task'})
            self.assertFalse(result.get('isError'), result)
            payload = json.loads(result['content'][0]['text'])
            self.assertFalse(payload['mappingVerified'])
            self.assertTrue(payload['observationOnly'])
            self.assertIsNone(payload['event']['contentReady'])
            self.assertIsNone(payload['event']['conditionMatched'])
            helper.request.assert_not_called()
        self.assertNotIn('cua_browser_events', server.TASK_TARGET_TOOLS)
        self.assertFalse(server.TOOL_MAP['cua_browser_events']['readOnly'])


if __name__ == '__main__':
    unittest.main()
