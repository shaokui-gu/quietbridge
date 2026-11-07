import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import browser_events
import server
from task_lifecycle import TaskOwner
import test_task_windows as fixtures
import test_browser_events as browser_fixtures


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TaskWindowTests('test_bind_truthful_minimal_persistent_identity')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def test_idle_timeout_revokes_binding_share_and_guard_without_new_helper(self):
        bound = self.fixture.bind()
        with server.task_store() as data:
            data['tasks'][bound['taskId']]['lease']['expiresAtUptime'] = time.monotonic() - 1
        self.fixture.fake.request.reset_mock()
        server.reap_task_lease()
        record = json.loads(self.fixture.path.read_text())['tasks'][bound['taskId']]
        self.assertEqual(record['state'], 'interrupted')
        self.assertEqual(record['interruptionReason'], 'idle_timeout')
        self.assertNotIn(bound['taskId'], server.TASK_BINDINGS)
        self.assertFalse(server.POWER_GUARD['active'])
        self.fixture.fake._stop.assert_called_once()
        self.fixture.fake.request.assert_not_called()
        result = self.fixture.rpc('cua_click', windowId=7, point={'x': 1, 'y': 1})
        self.assertEqual(result['structuredContent']['errorCode'], 'TASK_INTERRUPTED')
        result = self.fixture.rpc(action='bind', taskId=bound['taskId'], appHandle='a', windowId=7, confirm=True)
        self.assertEqual(result['structuredContent']['errorCode'], 'TASK_INTERRUPTED')

    def test_status_ping_and_browser_status_do_not_renew(self):
        bound = self.fixture.bind()
        before = json.loads(self.fixture.path.read_text())['tasks'][bound['taskId']]['lease']
        self.fixture.rpc(action='status')
        server.handle({'id': 'p', 'method': 'ping'})
        server.renew_task_lease('cua_permissions', {'action': 'status'})
        server.renew_task_lease('cua_browser_events', {'action': 'status', 'taskId': bound['taskId']})
        server.renew_task_lease('cua_browser_events', {'action': 'candidates'})
        after = json.loads(self.fixture.path.read_text())['tasks'][bound['taskId']]['lease']
        self.assertEqual(before, after)

    def test_actual_activity_renews_for_more_than_thirty_minutes(self):
        bound = self.fixture.bind()
        start = time.time()
        uptime = time.monotonic()
        for seconds in range(60, 2460, 60):
            with mock.patch.object(time, 'time', return_value=start + seconds), mock.patch.object(time, 'monotonic', return_value=uptime + seconds):
                server.reap_task_lease()
                server.renew_task_lease('cua_state', {'windowId': 7})
                task = json.loads(self.fixture.path.read_text())['tasks'][bound['taskId']]
                self.assertEqual(task['state'], 'bound')
                self.assertTrue(browser_events.lease_live(task, self.fixture.path.parent))
        self.fixture.fake._stop.assert_not_called()

    def test_historical_end_does_not_release_active_task(self):
        old = self.fixture.bind()
        new = self.fixture.payload(action='begin', interactionMode='desktop')
        self.fixture.payload(action='bind', taskId=new['taskId'], appHandle='a', windowId=8, confirm=True)
        self.fixture.fake.request.reset_mock()
        result = self.fixture.payload(action='end', taskId=old['taskId'], confirm=True)
        self.assertEqual(result['cleanup'], 'historical_record_only')
        self.assertEqual(json.loads(self.fixture.path.read_text())['activeTaskId'], new['taskId'])
        self.fixture.fake.request.assert_not_called()
        self.fixture.fake._stop.assert_not_called()

    def test_foreign_active_owner_cannot_be_ended_rebound_or_taken_over(self):
        bound = self.fixture.bind()
        other = TaskOwner()
        self.addCleanup(other.close)
        self.fixture.fake.request.reset_mock()
        with mock.patch.object(server, 'TASK_OWNER', other):
            for args in ({'action': 'begin'}, {'action': 'end', 'confirm': True},
                         {'action': 'bind', 'taskId': bound['taskId'], 'appHandle': 'a', 'windowId': 7, 'confirm': True}):
                result = self.fixture.rpc(**args)
                self.assertEqual(result['structuredContent']['errorCode'], 'TASK_IN_USE')
            status = self.fixture.payload(action='status')
            self.assertEqual(status['state'], 'bound')
            self.assertEqual(status['ownership'], 'another_connector')
            result = self.fixture.rpc('cua_browser_events', action='command', taskId=bound['taskId'], operation='snapshot')
            self.assertEqual(result['structuredContent']['errorCode'], 'TASK_IN_USE')
        self.fixture.fake.request.assert_not_called()

    def test_cancelled_rpc_interrupts_owned_task_without_replaying(self):
        task = self.fixture.bind()
        token = threading.Event()
        token.set()
        result = server.handle({'id': 1, 'method': 'tools/call', 'params': {'name': 'cua_click', 'arguments': {'windowId': 7}}}, token)
        self.assertEqual(result['result']['structuredContent']['errorCode'], 'TASK_CANCELLED')
        record = json.loads(self.fixture.path.read_text())['tasks'][task['taskId']]
        self.assertEqual(record['interruptionReason'], 'client_cancelled')
        self.fixture.fake._stop.assert_called_once()

    def test_cancel_during_native_wait_kills_proxy_without_retry(self):
        token = threading.Event()
        client = server.HelperClient('/never-start')
        proc = mock.Mock()
        client.proc = proc
        proc.poll.return_value = None
        def written(_):
            token.set()
        proc.stdin.write.side_effect = written
        with mock.patch.object(server, 'REQUEST_CANCELLED', token):
            with self.assertRaises(server.HelperError) as error:
                client.request('click', {}, timeout=10, retry_safe=False)
        self.assertEqual(error.exception.code, 'TASK_CANCELLED')
        proc.stdin.write.assert_called_once()
        proc.kill.assert_called_once()

    def test_browser_receipt_survives_native_renewal_failure(self):
        self.fixture.bind()
        self.fixture.fake.request.side_effect = server.HelperError('HELPER_BROKEN', 'fixture')
        server.renew_task_lease('cua_browser_events', {'action': 'command'})
        self.assertEqual(self.fixture.payload(action='status')['interruptionReason'], 'native_lease_renewal_failed')


class OwnerCrashTests(unittest.TestCase):
    def test_interruption_discards_unsent_creation_and_drains_late_report(self):
        fixture = browser_fixtures.BrowserEventsTests('test_assign_event_status_unbind_full_chain')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.await_window()
        fixture.sync()
        request = browser_events.internal_action({'action': 'create_target', 'taskId': 'task',
            'url': 'https://example.org'}, **fixture.paths)
        data = json.loads(fixture.tasks.read_text())
        data['tasks']['task']['state'] = 'interrupted'
        fixture.tasks.write_text(json.dumps(data))
        reply = fixture.sync()
        self.assertIsNone(reply['createRequest'])
        self.assertIsNone(reply['assignment'])
        reply = fixture.sync(created={'requestId': request['requestId'], 'browserWindowId': 5, 'tabId': 9})
        self.assertTrue(reply['ok'])
        self.assertIsNone(reply['assignment'])
        self.assertIsNone(reply['createRequest'])

    def test_kernel_unlock_on_sigkill_revokes_browser_assignment(self):
        fixture = browser_fixtures.BrowserEventsTests('test_assign_event_status_unbind_full_chain')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.assign()
        code = '''from task_lifecycle import TaskOwner
import json, sys
from pathlib import Path
owner = TaskOwner()
record = {}
owner.attach(record, Path(sys.argv[1]))
print(json.dumps(record), flush=True)
sys.stdin.read()
'''
        proc = subprocess.Popen([sys.executable, '-B', '-c', code, str(fixture.base)], cwd=Path(__file__).parent,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def close():
            if proc.poll() is None:
                proc.kill(); proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr): stream.close()
        self.addCleanup(close)
        lease_record = json.loads(proc.stdout.readline())
        data = json.loads(fixture.tasks.read_text())
        data['tasks']['task'].update(lease_record)
        fixture.tasks.write_text(json.dumps(data))
        self.assertIsNotNone(fixture.sync()['assignment'])
        proc.kill(); proc.wait(timeout=3)
        self.assertFalse(browser_events.lease_live(data['tasks']['task'], fixture.base))
        result = fixture.sync()
        self.assertIsNone(result['assignment'])
        self.assertIsNone(result['command'])

    def test_expired_and_malformed_lease_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = TaskOwner()
            self.addCleanup(owner.close)
            record = {}
            owner.attach(record, directory)
            self.assertTrue(browser_events.lease_live(record, directory))
            record['lease']['expiresAtUptime'] = time.monotonic() - 1
            self.assertFalse(browser_events.lease_live(record, directory))
            owner.renew(record)
            record['lease']['ownerId'] = '../bad'
            self.assertFalse(browser_events.lease_live(record, directory))
