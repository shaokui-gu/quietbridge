"""Core control has no experimental capture dependency; all state is isolated."""
import json
import unittest
from unittest import mock

import browser_events as events
import server
import test_task_windows as fixtures


class CoreBrowserFlowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TaskWindowTests('test_bind_truthful_minimal_persistent_identity')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def test_window_sharing_reads_no_experimental_preview_status(self):
        task = self.fixture.payload(action='begin', targetSurface='web_content')
        self.fixture.payload(action='bind', taskId=task['taskId'], appHandle='a', windowId=7, confirm=True)
        self.fixture.fake.request.reset_mock()
        with mock.patch.object(server, 'native_preview_matches', return_value=True):
            reply = self.fixture.rpc('quietbridge_windows', action='share', appHandle='a', windowId=7, source='window')
        self.assertFalse(reply.get('isError'), reply)
        requests = self.fixture.fake.request.call_args_list
        self.assertTrue(any(c.args == ('preview', {'action': 'hold', 'appHandle': 'a', 'windowId': 7,
                                                    'source': 'window', 'durationSeconds': 1800}) for c in requests))
        self.assertFalse(any(c.args[0] == 'preview' and c.args[1].get('action') == 'status' for c in requests))

    def test_stop_discards_legacy_experimental_capture_state(self):
        task = self.fixture.bind()
        path = self.fixture.path.with_name('browser-events.json')
        with events.event_store(path) as data:
            data['peer'] = {'windowPreview': {'targetOnCurrentSpace': False}, 'navigationCapture': {'status': 'captured'},
                            'previewLease': {'leaseId': 'old'}, 'previewFrame': {'data': 'discard'}, 'keep': True}
        self.fixture.payload(action='end', taskId=task['taskId'], confirm=True)
        peer = json.loads(path.read_text())['peer']
        self.assertEqual(peer, {'keep': True})
