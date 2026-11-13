import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from adapters.workbuddy.lifecycle import handle_hook
from lifecycle_store import store
from session_routing import issue, consume, SessionRouter, failure
from session_registry import reserve, claim_target, release, session_key, session_directory, registry, TargetInUse
from task_lifecycle import TaskOwner


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name)

    def test_prehook_binds_exact_arguments_and_token_is_single_use(self):
        payload = {'hook_event_name': 'PreToolUse', 'session_id': 'session-a',
                   'tool_name': 'mcp__quietbridge__quietbridge_task', 'tool_use_id': 'call-a',
                   'tool_input': {'action': 'begin', 'interactionMode': 'desktop'}}
        out = handle_hook(payload, self.config, 'turn-a')['hookSpecificOutput']
        self.assertEqual(out['updatedInput'], out['modifiedInput'])
        clean, context = consume(self.config, 'quietbridge_task', out['modifiedInput'])
        self.assertEqual(clean, payload['tool_input'])
        self.assertEqual(context['sessionId'], 'session-a')
        with self.assertRaisesRegex(ValueError, 'SESSION_ROUTING_INVALID'):
            consume(self.config, 'quietbridge_task', out['modifiedInput'])

    def test_public_entry_requires_host_context_and_preserves_core_schema(self):
        import server
        with mock.patch.object(server, 'SESSION_ROUTER', None), mock.patch.object(server, 'config_dir', return_value=self.config), \
                mock.patch.object(server, 'call_tool', side_effect=AssertionError('Core dispatch must not run')):
            result = server.handle({'id': 1, 'method': 'tools/call',
                'params': {'name': 'quietbridge_click', 'arguments': {'index': 1}}})['result']
            self.assertEqual(result['structuredContent']['errorCode'], 'SESSION_ROUTING_REQUIRED')
            self.assertEqual(result['structuredContent']['suggestedNext'], 'ask_user_choice')
            self.assertEqual(result['structuredContent']['recovery']['userDecision']['presentation'], 'agent_dialog')
            tools = server.handle({'id': 2, 'method': 'tools/list'})['result']['tools']
            self.assertTrue(all('_quietbridgeRoute' in t['inputSchema']['properties'] for t in tools))
            self.assertNotIn('_quietbridgeRoute', server.TOOL_MAP['quietbridge_click']['inputSchema']['properties'])

    def test_router_stop_and_unknown_result_use_the_same_recovery_contract(self):
        stopped = failure('TASK_CANCELLED: host stopped')['structuredContent']
        self.assertTrue(stopped['recovery']['taskMustEnd'])
        self.assertNotIn('userDecision', stopped['recovery'])
        unknown = failure('SESSION_TIMEOUT: missing result', 'unknown')['structuredContent']
        self.assertEqual(unknown['execution']['status'], 'unknown')
        self.assertTrue(unknown['recovery']['executionUncertain'])
        self.assertFalse(unknown['recovery']['replaySideEffects'])
        self.assertEqual(unknown['recovery']['onExhausted'], 'ask_user_choice')

    def test_alteration_expiry_and_wrong_tool_fail_before_dispatch(self):
        for mutate in ('arguments', 'tool', 'time'):
            args = issue(self.config, 'quietbridge_click', {'index': 1}, 'session-a', 'turn-a')
            with self.assertRaisesRegex(ValueError, 'SESSION_ROUTING_INVALID'):
                if mutate == 'arguments':
                    consume(self.config, 'quietbridge_click', dict(args, index=2))
                elif mutate == 'tool':
                    consume(self.config, 'quietbridge_key', args)
                else:
                    with mock.patch('session_routing.time.time', return_value=time.time() + 91):
                        consume(self.config, 'quietbridge_click', args)

    def test_stop_tombstone_cannot_be_bypassed_by_late_prehook(self):
        handle_hook({'hook_event_name': 'FinalStop', 'session_id': 'session-a'}, self.config, 'turn-a')
        args = issue(self.config, 'quietbridge_task', {'action': 'begin'}, 'session-a', 'turn-a')
        with self.assertRaisesRegex(ValueError, 'TASK_CANCELLED'):
            consume(self.config, 'quietbridge_task', args)
        fresh = issue(self.config, 'quietbridge_task', {'action': 'begin'}, 'session-a', 'turn-b')
        self.assertEqual(consume(self.config, 'quietbridge_task', fresh)[1]['requestId'], 'turn-b')

    def test_real_processes_keep_b_and_c_alive_after_a_crashes(self):
        bridge = self.config / 'worker.py'
        bridge.write_text('''import json,os,sys,time
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')!='tools/call': continue
 a=m['params']['arguments']
 if a.get('crash'): os._exit(7)
 print(json.dumps({'id':m['id'],'result':{'pid':os.getpid(),'key':os.environ['QUIETBRIDGE_SESSION_KEY'],'value':a}}),flush=True)
''')
        router = SessionRouter(bridge, self.config)
        self.addCleanup(router.close)
        def call(session, **args):
            token = issue(self.config, 'quietbridge_task', args, session, 'turn-1')
            return router.call({'id': session, 'method': 'tools/call',
                'params': {'name': 'quietbridge_task', 'arguments': token}})
        replies = {s: call(s, value=s) for s in ('a', 'b', 'c')}
        self.assertEqual(len({r['pid'] for r in replies.values()}), 3)
        self.assertEqual(call('d')['structuredContent']['errorCode'], 'SESSION_CAPACITY_FULL')
        crashed = call('a', crash=True)['structuredContent']
        self.assertEqual(crashed['errorCode'], 'SESSION_WORKER_EXITED')
        self.assertEqual(crashed['execution']['status'], 'unknown')
        self.assertFalse(crashed['recovery']['taskMustEnd'])
        for s in ('b', 'c'):
            self.assertEqual(call(s, value='still-alive')['pid'], replies[s]['pid'])
        handle_hook({'hook_event_name': 'FinalStop', 'session_id': 'a'}, self.config, 'turn-1')
        router.reap()
        self.assertNotIn(session_key('a'), router.workers)
        self.assertIn('pid', call('d'))


class RegistryTests(unittest.TestCase):
    setUp = RoutingTests.setUp
    def slot(self, session):
        key = session_key(session)
        directory = session_directory(self.config, key, create=True)
        owner = TaskOwner()
        self.addCleanup(owner.close)
        record = {'taskId': session, 'state': 'awaiting_window'}
        owner.attach(record, directory)
        task_file = directory / 'task-windows.json'
        reserve(self.config, key, task_file, record)
        task_file.write_text(json.dumps({'version': 1, 'activeTaskId': session, 'tasks': {session: record}}))
        return key, task_file, record

    def test_capacity_target_conflict_and_owner_death_are_isolated(self):
        slots = [self.slot(s) for s in ('a', 'b', 'c')]
        with self.assertRaisesRegex(ValueError, 'SESSION_CAPACITY_FULL'):
            self.slot('d')
        key, path, record = slots[0]
        claim_target(self.config, key, dict(record, pid=10, windowId=100, interactionMode='desktop'), path)
        key_b, path_b, record_b = slots[1]
        with self.assertRaisesRegex(ValueError, 'TARGET_IN_USE'):
            claim_target(self.config, key_b, dict(record_b, pid=10, windowId=101, interactionMode='desktop'), path_b)
        claim_target(self.config, key_b, dict(record_b, pid=20, windowId=200, interactionMode='desktop'), path_b)
        release(self.config, key, record['taskId'])
        with registry(self.config) as live:
            self.assertEqual(set(live), {slots[1][0], slots[2][0]})
        self.slot('d')

    def test_conflict_identifies_exact_owner_without_titles_or_other_session_data(self):
        key, path, record = self.slot('collector')
        key_b, path_b, record_b = self.slot('test')
        claim_target(self.config, key, dict(record, pid=10, windowId=274431, interactionMode='background_browser'), path)
        with store(self.config) as data:
            data['turns']['collector-run'] = {'sessionId': 'collector', 'requestId': 'run-a',
                'tasks': {record['taskId']: record['lease']['ownerId']}, 'updatedAt': time.time(),
                'private': 'must not be exposed'}
        with self.assertRaises(TargetInUse) as caught:
            claim_target(self.config, key_b, dict(record_b, pid=10, windowId=274431,
                                                 interactionMode='background_browser'), path_b)
        conflict = caught.exception.conflict
        self.assertEqual(conflict, {'taskId': record['taskId'], 'scope': 'window', 'pid': 10,
            'windowId': 274431, 'interactionMode': 'background_browser', 'hostSessionId': 'collector'})
        self.assertIn('collector', str(caught.exception))
        self.assertNotIn('must not be exposed', str(caught.exception))
        # Chrome windows in the same process remain independent.
        claim_target(self.config, key_b, dict(record_b, pid=10, windowId=275157,
                                             interactionMode='background_browser'), path_b)
        with self.assertRaises(TargetInUse) as caught:
            claim_target(self.config, key_b, dict(record_b, pid=10, windowId=275157,
                                                 interactionMode='desktop'), path_b)
        self.assertEqual(caught.exception.conflict['scope'], 'application_process')

    def test_historical_slot_cannot_keep_a_window_occupied(self):
        key, path, record = self.slot('collector')
        claim_target(self.config, key, dict(record, pid=10, windowId=274431, interactionMode='background_browser'), path)
        data = json.loads(path.read_text())
        data['activeTaskId'] = None
        path.write_text(json.dumps(data))
        with registry(self.config) as live:
            self.assertNotIn(key, live)

    def test_panel_stop_blocks_same_run_reacquisition_without_stopping_other_sessions(self):
        key, path, record = self.slot('collector')
        key_b, path_b, record_b = self.slot('test')
        with store(self.config) as data:
            data['turns']['collector-run'] = {'sessionId': 'collector', 'requestId': 'run-a',
                'tasks': {record['taskId']: record['lease']['ownerId']},
                'stoppedAt': time.time(), 'updatedAt': time.time(), 'reason': 'user_stopped_in_quietbridge'}
        token = issue(self.config, 'quietbridge_task', {'action': 'begin'}, 'collector', 'run-a')
        with self.assertRaisesRegex(ValueError, 'TASK_CANCELLED'):
            consume(self.config, 'quietbridge_task', token)
        token = issue(self.config, 'quietbridge_task', {'action': 'begin'}, 'test', 'run-a')
        self.assertEqual(consume(self.config, 'quietbridge_task', token)[1]['sessionId'], 'test')
        token = issue(self.config, 'quietbridge_task', {'action': 'begin'}, 'collector', 'new-user-turn')
        self.assertEqual(consume(self.config, 'quietbridge_task', token)[1]['requestId'], 'new-user-turn')


if __name__ == '__main__':
    unittest.main()
