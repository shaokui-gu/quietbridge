"""Window closure is recoverable; explicit stops and unknown inputs are not replayed."""
import json
import unittest
from unittest import mock

import browser_events as events
import server
import test_task_windows as fixtures


class TargetRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TaskWindowTests('test_bind_truthful_minimal_persistent_identity')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        patcher = mock.patch.object(server, 'application_session', return_value='live-app')
        patcher.start()
        self.addCleanup(patcher.stop)

    def bind_created(self, task=None, window=7):
        f = self.fixture
        task = task or f.payload(action='begin', targetSurface='web_content')
        task = f.payload(action='bind', taskId=task['taskId'], appHandle='a', windowId=window, confirm=True)
        path = f.path.with_name('browser-events.json')
        with events.event_store(path) as data:
            data['tasks'][task['taskId']] = {
                'taskId': task['taskId'], 'browserWindowId': 101, 'tabId': 102,
                'targetIdentity': events.target_identity(task), 'mappingVerified': True,
                'mapping': {'windowId': window},
                'targetEvidence': {'mode': 'created', 'createdRequestId': 'request-1'}}
            data['peer'] = {'createdTargets': [{'taskId': task['taskId'], 'requestId': 'request-1',
                             'browserWindowId': 101, 'tabId': 102}]}
        server.remember_created_browser_window(task['taskId'])
        return task

    def close(self, task):
        f = self.fixture
        f.windows = [w for w in f.windows if w['windowId'] != task['windowId']]
        f.fake.request.reset_mock()
        return f.payload(action='status', taskId=task['taskId'])

    def test_confirmed_closure_returns_plan_without_creating_or_taking_another_window(self):
        task = self.bind_created()
        status = self.close(task)
        self.assertEqual(status['state'], 'invalidated')
        self.assertEqual(status['targetRecovery']['status'], 'recreate_allowed')
        self.assertTrue(status['targetRecovery']['closedWindowConfirmed'])
        self.assertEqual(status['recovery']['action'], 'recreate_closed_task_window')
        self.assertFalse(status['recovery']['waitForUser'])
        self.assertEqual(self.fixture.state['app'], 'a')
        self.assertEqual([w['windowId'] for w in self.fixture.windows], [8])
        methods = [c.args[0] for c in self.fixture.fake.request.call_args_list]
        self.assertTrue(set(methods) <= {'browserSession', 'windowIdentity', 'listWindows', 'preview'})

    def test_existing_user_window_is_not_automatically_recreated(self):
        task = self.fixture.payload(action='begin', targetSurface='web_content')
        task = self.fixture.payload(action='bind', taskId=task['taskId'], appHandle='a', windowId=7, confirm=True)
        status = self.close(task)
        self.assertEqual(status['targetRecovery']['status'], 'inspect_required')
        self.assertEqual(status['recovery']['action'], 'inspect_lost_target')

    def test_window_still_present_or_process_unverified_never_authorizes_creation(self):
        task = self.bind_created()
        with server.task_store() as data:
            data['tasks'][task['taskId']]['state'] = 'invalidated'
        status = self.fixture.payload(action='status')
        self.assertEqual(status['targetRecovery']['reason'], 'original_window_still_exists')
        original = self.fixture.request
        def changed(method, params=None, **kwargs):
            if method == 'windowIdentity':
                raise server.HelperError('STALE_APP', 'process identity changed')
            return original(method, params, **kwargs)
        self.fixture.fake.request.side_effect = changed
        status = self.fixture.payload(action='status')
        self.assertEqual(status['targetRecovery']['reason'], 'original_process_unverified')
        self.assertNotIn('windowClosure', status)

    def test_forged_or_unverified_mapping_does_not_record_creation_provenance(self):
        task = self.bind_created()
        with server.task_store() as data:
            data['tasks'][task['taskId']].pop('windowCreation')
        with events.event_store(self.fixture.path.with_name('browser-events.json')) as data:
            data['tasks'][task['taskId']]['mapping']['windowId'] = 8
        server.remember_created_browser_window(task['taskId'])
        self.assertNotIn('windowCreation', self.fixture.payload(action='status'))

    def test_recovery_chain_allows_two_replacements_then_dialog_without_cancel(self):
        f = self.fixture
        task = self.bind_created()
        for attempt, window in ((1, 9), (2, 10)):
            status = self.close(task)
            self.assertEqual(status['targetRecovery']['status'], 'recreate_allowed')
            f.payload(action='end', taskId=task['taskId'], confirm=True)
            successor = f.payload(action='begin', targetSurface='web_content', recoveryOf=task['taskId'])
            self.assertEqual(successor['windowRecovery']['attempt'], attempt)
            repeat = f.payload(action='begin', targetSurface='web_content', recoveryOf=task['taskId'])
            self.assertEqual(repeat['taskId'], successor['taskId'], 'Repeated receipt lookup must reuse the phase')
            f.windows.append({'windowId': window, 'pid': 88})
            task = self.bind_created(successor, window)
        status = self.close(task)
        self.assertEqual(status['targetRecovery']['status'], 'decision_required')
        self.assertFalse(status['recovery']['taskMustEnd'])
        self.assertTrue(status['recovery']['waitForUser'])
        self.assertEqual(status['recovery']['userDecision']['preferredTool'], 'AskUserQuestion')
        f.payload(action='end', taskId=task['taskId'], confirm=True)
        result = f.rpc(action='begin', targetSurface='web_content', recoveryOf=task['taskId'])
        self.assertEqual(result['structuredContent']['errorCode'], 'RECOVERY_DECISION_REQUIRED')
        self.assertEqual(result['structuredContent']['suggestedNext'], 'ask_user_choice')

    def test_stopped_host_and_expired_closure_cannot_create_recovery_phase(self):
        f = self.fixture
        task = self.bind_created()
        self.close(task)
        f.payload(action='end', taskId=task['taskId'], confirm=True)
        with mock.patch.object(server, 'host_stop_reason', return_value='host_final_stop_cancelled'):
            result = f.rpc(action='begin', targetSurface='web_content', recoveryOf=task['taskId'])
        self.assertEqual(result['structuredContent']['errorCode'], 'TASK_INTERRUPTED')
        self.assertTrue(result['structuredContent']['recovery']['taskMustEnd'])
        self.assertNotIn('userDecision', result['structuredContent']['recovery'])
        with server.task_store() as data:
            data['tasks'][task['taskId']]['windowClosure']['checkedAt'] -= 121
        result = f.rpc(action='begin', targetSurface='web_content', recoveryOf=task['taskId'])
        self.assertEqual(result['structuredContent']['errorCode'], 'REQUIRES_REVALIDATION')

    def test_pending_old_input_is_not_replayed_by_recovery(self):
        task = self.bind_created()
        path = self.fixture.path.with_name('browser-events.json')
        with events.event_store(path) as data:
            data['peer']['commandTerminals'] = [{'commandId': 'old-click', 'status': 'expired',
                'taskId': task['taskId'], 'mayHaveExecuted': True}]
        self.close(task)
        self.fixture.payload(action='end', taskId=task['taskId'], confirm=True)
        self.fixture.payload(action='begin', targetSurface='web_content', recoveryOf=task['taskId'])
        self.assertTrue(json.loads(path.read_text())['peer']['commandTerminals'][0]['mayHaveExecuted'])
        self.assertFalse(any(c.args[0] in ('click', 'pressKey', 'typeText')
                             for c in self.fixture.fake.request.call_args_list))

    def test_stale_handle_requests_revalidation_instead_of_permanent_invalidation(self):
        task = self.fixture.bind()
        original = self.fixture.request
        def stale(method, params=None, **kwargs):
            if method == 'windowIdentity':
                raise server.HelperError('STALE_APP', 'refresh original handle')
            return original(method, params, **kwargs)
        self.fixture.fake.request.side_effect = stale
        self.assertEqual(self.fixture.payload(action='status')['state'], 'requires_revalidation')

    def test_recovery_receipt_cannot_reuse_an_ended_or_changed_scope_successor(self):
        f = self.fixture
        task = self.bind_created()
        self.close(task)
        f.payload(action='end', taskId=task['taskId'], confirm=True)
        successor = f.payload(action='begin', targetSurface='web_content', recoveryOf=task['taskId'])
        with mock.patch.object(server, 'application_session', return_value='new-app'):
            result = f.rpc(action='begin', targetSurface='web_content', recoveryOf=task['taskId'])
        self.assertEqual(result['structuredContent']['errorCode'], 'REQUIRES_REVALIDATION')
        f.payload(action='end', taskId=successor['taskId'], confirm=True)
        result = f.rpc(action='begin', targetSurface='web_content', recoveryOf=task['taskId'])
        self.assertEqual(result['structuredContent']['errorCode'], 'REQUIRES_REVALIDATION')
        self.assertEqual(f.payload(action='status')['state'], 'idle')

    def test_recovery_lineage_is_only_accepted_on_begin(self):
        schema = server.TOOL_MAP['quietbridge_task']['inputSchema']
        self.assertIn('recoveryOf', schema['properties'])
        for action in ('bind', 'status', 'end'):
            result = self.fixture.rpc(action=action, recoveryOf='old', confirm=True, appHandle='a', windowId=7)
            self.assertEqual(result['structuredContent']['errorCode'], 'INVALID_ARGUMENT')
