"""A fresh session can inspect extension presence before reserving any task slot."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import browser_events as events
import server


class BrowserPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.scoped = self.root / 'sessions' / ('a' * 64)
        self.scoped.mkdir(parents=True)
        self.tasks = self.scoped / 'task-windows.json'
        self.global_events = self.root / 'browser-events.json'
        self.private_events = self.scoped / 'browser-events.json'
        self.peer = {'applicationSession': 'app-session', 'lastSeen': time.time(),
                     'enabled': True, 'permission': True, 'clientVersion': '0.4.6',
                     'candidatesAt': time.time(),
                     'candidates': [{'browserWindowId': 10, 'tabId': 11, 'url': 'https://example.org/'}]}
        self.write(self.global_events, {'version': 1, 'tasks': {'other': {'sentinel': True}}, 'peer': self.peer})
        self.patch(events, 'application_session', return_value='app-session')
        self.patch(events, 'EVENT_FILE', self.private_events)
        self.patch(events, 'CANDIDATE_QUERY_TIMEOUT', 0)
        self.patch(server, 'task_root', return_value=self.root)

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def test_permissions_presence_does_not_require_a_private_task_channel(self):
        self.assertTrue(server.browser_channel_state())
        self.assertFalse(self.private_events.exists())
        self.assertFalse(self.tasks.exists())

    def test_foreign_or_stale_global_heartbeat_does_not_pass_presence(self):
        for change in ({'applicationSession': 'old-app'}, {'lastSeen': time.time()-30},
                       {'lastSeen': time.time()+30}, {'enabled': False}, {'permission': False}):
            with self.subTest(change=change):
                self.write(self.global_events, {'version': 1, 'tasks': {}, 'peer': dict(self.peer, **change)})
                self.assertFalse(server.browser_channel_state())

    def test_candidates_without_task_queries_global_metadata_only(self):
        tool = events.make_tool(lambda: self.tasks, preflight_root=lambda: self.root)
        result = json.loads(tool['handler']({'action': 'candidates'}))
        self.assertEqual(result['nativeConnection'], 'connected')
        self.assertEqual(result['clientVersion'], '0.4.6')
        self.assertEqual(result['connectionScope'], 'extension_preflight')
        self.assertFalse(result['taskChannelReady'])
        self.assertEqual(result['candidates'], self.peer['candidates'])
        data = json.loads(self.global_events.read_text())
        self.assertEqual(data['tasks'], {'other': {'sentinel': True}})
        self.assertTrue(data['peer']['requestCandidates'])
        self.assertFalse(self.tasks.exists())
        self.assertFalse(self.private_events.exists())
        self.assertFalse((self.root / 'session-registry.json').exists())

    def test_active_task_never_uses_global_candidates_as_private_evidence(self):
        self.write(self.tasks, {'version': 1, 'activeTaskId': 'own-task',
            'tasks': {'own-task': {'taskId': 'own-task', 'state': 'awaiting_window'}}})
        self.patch(events, 'lease_live', return_value=True)
        self.patch(events, 'internal_action', return_value={'nativeConnection': 'host_unavailable',
                   'candidates': [], 'refreshPending': False})
        tool = events.make_tool(lambda: self.tasks, preflight_root=lambda: self.root)
        result = json.loads(tool['handler']({'action': 'candidates'}))
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['connectionScope'], 'task_channel')
        self.assertEqual(result['extensionConnection'], 'connected')
        self.assertFalse(result['taskChannelReady'])
        self.assertEqual(result['refreshStatus'], 'channel_pending')
        self.assertTrue(result['refreshPending'])
        self.assertEqual(result['retryAfterMs'], 1000)

    def own_task(self):
        self.write(self.tasks, {'version': 1, 'activeTaskId': 'own-task',
            'tasks': {'own-task': {'taskId': 'own-task', 'state': 'awaiting_window'}}})
        self.patch(events, 'lease_live', return_value=True)

    def test_task_candidates_are_collected_from_private_peer_after_first_sync(self):
        self.own_task()
        private_peer = dict(self.peer, candidates=[{'browserWindowId': 20, 'tabId': 21, 'url': 'https://example.org/private'}])
        self.patch(events, 'CANDIDATE_QUERY_TIMEOUT', 1)
        self.patch(events.time, 'sleep', side_effect=lambda _: self.write(self.private_events,
            {'version': 1, 'tasks': {}, 'peer': private_peer}))
        tool = events.make_tool(lambda: self.tasks, preflight_root=lambda: self.root)
        result = json.loads(tool['handler']({'action': 'candidates', 'taskId': 'own-task'}))
        self.assertEqual(result['candidates'], private_peer['candidates'])
        self.assertTrue(result['taskChannelReady'])
        self.assertEqual(result['connectionScope'], 'task_channel')
        self.assertNotIn('executionPolicy', result)

    def test_create_during_channel_start_does_not_queue_input_or_use_root(self):
        self.own_task()
        tool = events.make_tool(lambda: self.tasks, preflight_root=lambda: self.root)
        with mock.patch.object(events, 'internal_action', side_effect=AssertionError('must not dispatch')):
            with self.assertRaisesRegex(ValueError, 'BROWSER_CHANNEL_STARTING'):
                tool['handler']({'action': 'create_target', 'taskId': 'own-task'})
        self.assertFalse(self.private_events.exists())
        self.assertNotIn('createRequest', json.loads(self.global_events.read_text())['peer'])

    def test_fresh_private_permission_denial_is_not_reported_as_channel_startup(self):
        self.own_task()
        self.write(self.private_events, {'version': 1, 'tasks': {}, 'peer': dict(self.peer, permission=False)})
        tool = events.make_tool(lambda: self.tasks, preflight_root=lambda: self.root)
        result = json.loads(tool['handler']({'action': 'candidates', 'taskId': 'own-task'}))
        self.assertEqual(result['nativeConnection'], 'connected')
        self.assertFalse(result['globalPermission'])
        self.assertFalse(result['taskChannelReady'])
        self.assertNotEqual(result['refreshStatus'], 'channel_pending')
        with self.assertRaisesRegex(ValueError, 'BROWSER_PERMISSION_MISSING'):
            tool['handler']({'action': 'create_target', 'taskId': 'own-task'})
        self.assertNotIn('createRequest', json.loads(self.private_events.read_text())['peer'])

    def test_cancelled_channel_wait_does_not_dispatch(self):
        self.own_task()
        def cancelled():
            raise ValueError('TASK_CANCELLED')
        tool = events.make_tool(lambda: self.tasks, check_cancelled=cancelled, preflight_root=lambda: self.root)
        with mock.patch.object(events, 'internal_action', side_effect=AssertionError('must not dispatch')):
            with self.assertRaisesRegex(ValueError, 'TASK_CANCELLED'):
                tool['handler']({'action': 'candidates', 'taskId': 'own-task'})
        self.assertFalse(self.private_events.exists())

    def test_explicit_unknown_task_never_falls_back_to_global(self):
        original = self.global_events.read_bytes()
        tool = events.make_tool(lambda: self.tasks, preflight_root=lambda: self.root)
        with self.assertRaisesRegex(ValueError, '当前活动任务'):
            tool['handler']({'action': 'candidates', 'taskId': 'missing'})
        self.assertEqual(self.global_events.read_bytes(), original)

    def test_mismatched_application_session_cannot_publish_preflight_candidates(self):
        self.write(self.global_events, {'version': 1, 'tasks': {},
                                       'peer': dict(self.peer, applicationSession='previous-app')})
        tool = events.make_tool(lambda: self.tasks, preflight_root=lambda: self.root)
        result = json.loads(tool['handler']({'action': 'candidates'}))
        self.assertEqual(result['nativeConnection'], 'host_unavailable')
        self.assertIsNone(result['clientVersion'])
        self.assertEqual(result['candidates'], [])


if __name__ == '__main__':
    unittest.main()
