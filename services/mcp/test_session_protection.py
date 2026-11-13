"""Session protection tests use fake clocks and helpers; no real desktop I/O."""
import copy
import json
import time
import unittest
from unittest import mock

from adapters.workbuddy.lifecycle import handle_hook
from task_lifecycle import ProtectionSession
import server
import test_task_windows as fixtures


class ProtectionClockTests(unittest.TestCase):
    def setUp(self):
        self.wall, self.uptime = 10000.0, 5000.0
        for patcher in (mock.patch('task_lifecycle.time.time', side_effect=lambda: self.wall),
                        mock.patch('task_lifecycle.time.monotonic', side_effect=lambda: self.uptime)):
            patcher.start(); self.addCleanup(patcher.stop)
        self.session = ProtectionSession()
        self.session.attach({'taskId': 'phase-1'})

    def advance(self, seconds):
        self.wall += seconds; self.uptime += seconds

    def test_unacknowledged_connector_expires_in_120_seconds(self):
        self.advance(120)
        self.assertEqual(self.session.remaining(), 0)

    def test_status_reads_and_same_receipt_cannot_renew_the_30_minute_limit(self):
        receipt = {'lastActivityAt': self.wall}
        self.assertEqual(self.session.remaining(receipt), 1800)
        for _ in range(30):
            self.advance(60)
            remaining = self.session.remaining(receipt)
        self.assertEqual(remaining, 0)

    def test_real_business_receipts_extend_the_run_and_stop_revokes_it(self):
        receipt = {'lastActivityAt': self.wall}
        for _ in range(60):
            self.advance(60)
            receipt['lastActivityAt'] = self.wall
            self.assertEqual(self.session.remaining(receipt), 1800)
        self.assertEqual(self.session.remaining(dict(receipt, stoppedAt=self.wall)), 0)

    def test_clock_rollback_and_sleep_do_not_make_a_lease_unbounded(self):
        receipt = {'lastActivityAt': self.wall}
        self.session.remaining(receipt)
        self.wall -= 5000
        self.uptime += 1801
        self.assertEqual(self.session.remaining(receipt), 0)

    def test_delayed_receipt_uses_its_actual_activity_time(self):
        receipt = {'lastActivityAt': self.wall}
        self.advance(600)
        # Local activity is not newer than the delayed receipt.
        self.assertEqual(self.session.remaining(receipt), 1200)


class ProtectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TaskWindowTests('test_bind_truthful_minimal_persistent_identity')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def bind(self):
        bound = self.fixture.bind()
        self.fixture.acknowledge(bound)
        return bound

    def stop(self, session='session-a', request='run-a'):
        handle_hook({'session_id': session, 'hook_event_name': 'FinalStop',
                     'final_stop_reason': 'cancelled'}, self.fixture.path.parent, request)

    def test_phase_end_keeps_protection_but_host_stop_still_closes_connection(self):
        task = self.bind()
        self.fixture.fake.request.reset_mock()
        result = self.fixture.payload(action='end', taskId=task['taskId'], confirm=True)
        self.assertTrue(result['sessionProtection']['retained'])
        self.assertIsNone(json.loads(self.fixture.path.read_text())['activeTaskId'])
        self.assertEqual(server.SESSION_PROTECTION.record['taskId'], task['taskId'])
        self.assertEqual(self.fixture.fake.request.call_args_list[0].args, ('preview', {'action': 'release'}))
        self.fixture.fake._stop.assert_not_called()
        self.stop()
        self.assertEqual(server.pending_host_stop(), 'host_final_stop_cancelled')
        server.reap_protection_session()
        self.fixture.fake._stop.assert_called_once()
        self.assertFalse(server.POWER_GUARD['active'])

    def test_other_session_stop_and_turn_stop_do_not_close_this_run(self):
        self.bind()
        self.stop(session='session-b'); self.stop(request='other-run')
        self.assertIsNone(server.pending_host_stop())
        server.reap_protection_session()
        self.assertTrue(server.protection_status()['retained'])
        self.fixture.fake._stop.assert_not_called()

    def test_target_lease_stays_claimed_until_native_connection_closes(self):
        from session_registry import live_slots
        task = self.bind()
        def closing():
            record = json.loads(self.fixture.path.read_text())['tasks'][task['taskId']]
            self.assertEqual(record['state'], 'stopping')
            with server.task_store() as data:
                data['tasks'][task['taskId']]['lease']['expiresAt'] = time.time() - 1
                data['tasks'][task['taskId']]['lease']['expiresAtUptime'] = time.monotonic() - 1
            slots = live_slots(json.loads(self.fixture.path.with_name('session-registry.json').read_text()))
            self.assertTrue(any(slot['taskId'] == task['taskId'] for slot in slots.values()),
                            'A new writer cannot claim the window during native cleanup')
        self.fixture.fake._stop.side_effect = closing
        server.interrupt_task('user_stopped_in_quietbridge')
        record = json.loads(self.fixture.path.read_text())['tasks'][task['taskId']]
        self.assertEqual(record['state'], 'interrupted')
        self.assertEqual(live_slots(json.loads(self.fixture.path.with_name('session-registry.json').read_text())), {})

    def test_same_run_can_start_another_phase_without_restarting_protection(self):
        first = self.bind()
        self.fixture.payload(action='end', taskId=first['taskId'], confirm=True)
        fresh = self.fixture.payload(action='begin', interactionMode='desktop')
        self.fixture.acknowledge(fresh)
        self.fixture.fake.request.reset_mock()
        self.fixture.payload(action='bind', taskId=fresh['taskId'], appHandle='a', windowId=8, confirm=True)
        self.assertEqual(server.SESSION_PROTECTION.record['taskId'], first['taskId'])
        self.assertFalse(any(call.args[0] == 'powerGuard' for call in self.fixture.fake.request.call_args_list))
        self.stop(); server.reap_protection_session()
        self.assertEqual(json.loads(self.fixture.path.read_text())['tasks'][fresh['taskId']]['state'], 'interrupted')

    def test_new_phase_with_missing_or_different_identity_never_reaches_native(self):
        first = self.bind()
        self.fixture.payload(action='end', taskId=first['taskId'], confirm=True)
        fresh = self.fixture.payload(action='begin', interactionMode='desktop')
        self.fixture.fake.request.reset_mock()
        args = dict(action='bind', taskId=fresh['taskId'], appHandle='a', windowId=8, confirm=True)
        for session in (None, 'session-b'):
            if session:
                self.fixture.acknowledge(fresh, session=session)
            result = self.fixture.rpc(**args)
            self.assertEqual(result['structuredContent']['errorCode'], 'SESSION_ROUTING_REQUIRED')
            self.assertTrue(result['structuredContent']['recovery']['waitForUser'])
            self.fixture.fake.request.assert_not_called()
            self.fixture.fake._stop.assert_not_called()
            self.assertTrue(server.protection_status()['retained'])

    def test_unknown_connector_scopes_cannot_inherit_each_other(self):
        first = self.fixture.bind()
        self.fixture.payload(action='end', taskId=first['taskId'], confirm=True)
        fresh = self.fixture.payload(action='begin', interactionMode='desktop')
        self.fixture.fake.request.reset_mock()
        result = self.fixture.rpc(action='bind', taskId=fresh['taskId'], appHandle='a', windowId=8, confirm=True)
        self.assertEqual(result['structuredContent']['errorCode'], 'SESSION_ROUTING_REQUIRED')
        self.fixture.fake.request.assert_not_called()

    def test_phase_idle_closes_preview_without_ending_acknowledged_run(self):
        bound = self.bind()
        with server.task_store() as data:
            data['tasks'][bound['taskId']]['lease']['expiresAtUptime'] = time.monotonic() - 1
        self.fixture.fake.request.reset_mock()
        server.reap_task_lease()
        self.assertTrue(server.POWER_GUARD['active'])
        self.fixture.fake._stop.assert_not_called()
        self.assertEqual(self.fixture.fake.request.call_args_list[0].args, ('preview', {'action': 'release'}))
        self.assertEqual(json.loads(self.fixture.path.read_text())['tasks'][bound['taskId']]['state'], 'suspended')

    def test_watchdog_renews_native_protection_without_renewing_activity(self):
        self.bind()
        before = copy.deepcopy(vars(server.SESSION_PROTECTION))
        server.POWER_GUARD['nextRenewAt'] = 0
        self.fixture.fake.request.reset_mock()
        server.reap_protection_session()
        self.assertEqual(before['activity_at'], server.SESSION_PROTECTION.activity_at)
        self.assertEqual(before['activity_uptime'], server.SESSION_PROTECTION.activity_uptime)
        self.assertEqual(self.fixture.fake.request.call_args.args[0], 'powerGuard')

    def test_missing_hooks_expire_after_end_without_status_extending_lifetime(self):
        task = self.fixture.bind()
        self.fixture.payload(action='end', taskId=task['taskId'], confirm=True)
        server.SESSION_PROTECTION.activity_uptime -= 121
        self.assertEqual(server.protection_status()['remainingSeconds'], 0)
        server.reap_protection_session()
        self.fixture.fake._stop.assert_called_once()
        self.assertIsNone(server.SESSION_PROTECTION.record)

    def test_stopped_run_cannot_bind_before_the_next_watchdog_tick(self):
        task = self.fixture.payload(action='begin', interactionMode='desktop')
        self.fixture.acknowledge(task)
        self.stop()
        self.fixture.fake.request.reset_mock()
        result = self.fixture.rpc(action='bind', taskId=task['taskId'], appHandle='a', windowId=7, confirm=True)
        self.assertEqual(result['structuredContent']['errorCode'], 'TASK_INTERRUPTED')
        self.fixture.fake.request.assert_not_called()
