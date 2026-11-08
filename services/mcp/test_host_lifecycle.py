import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from adapters.workbuddy import lifecycle


class HostLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name).resolve()
        self.session = str(uuid.uuid4())
        self.task = {'taskId': str(uuid.uuid4()), 'state': 'bound',
                     'lease': {'ownerId': str(uuid.uuid4()), 'expiresAt': 1120, 'idleSeconds': 120}}
        self.publish(self.task)

    def publish(self, task):
        (self.config / 'task-windows.json').write_text(json.dumps(
            {'version': 1, 'activeTaskId': task['taskId'], 'tasks': {task['taskId']: task}}))

    def register(self, task=None, session=None, request='turn-1', now=1001):
        lifecycle.handle_hook({'session_id': session or self.session, 'hook_event_name': 'PostToolUse',
            'tool_name': 'mcp__quietbridge__quietbridge_task', 'tool_input': {'action': 'begin'},
            'tool_response': {'content': [{'type': 'text', 'text': json.dumps(task or self.task)}]}},
            self.config, request, now)

    def stop(self, session=None, request='turn-1', now=1010, event='FinalStop'):
        lifecycle.handle_hook({'session_id': session or self.session, 'hook_event_name': event,
            'final_stop_reason': 'cancelled'}, self.config, request, now)

    def test_final_stop_revokes_exact_task_and_owner(self):
        self.register(); self.stop()
        self.assertEqual(lifecycle.stop_reason(self.task, self.config), 'host_final_stop_cancelled')
        wrong = dict(self.task, lease=dict(self.task['lease'], ownerId=str(uuid.uuid4())))
        self.assertIsNone(lifecycle.stop_reason(wrong, self.config))
        self.assertIsNone(lifecycle.stop_reason(dict(self.task, taskId=str(uuid.uuid4())), self.config))

    def test_stop_still_handles_receipts_from_the_legacy_tool_name(self):
        lifecycle.handle_hook({'session_id': self.session, 'hook_event_name': 'PostToolUse',
            'tool_name': 'mcp__workbuddy-cua__cua_task', 'tool_input': {'action': 'bind'},
            'tool_response': self.task}, self.config, 'turn-1', 1001)
        self.stop()
        self.assertEqual(lifecycle.stop_reason(self.task, self.config), 'host_final_stop_cancelled')

    def test_other_session_or_turn_cannot_stop_the_active_task(self):
        self.register()
        self.stop(session=str(uuid.uuid4()))
        self.stop(request='old-turn')
        self.assertIsNone(lifecycle.stop_reason(self.task, self.config))

    def test_stop_before_post_tool_receipt_does_not_lose_cancellation(self):
        self.stop()
        self.register(now=1011)
        self.assertEqual(lifecycle.stop_reason(self.task, self.config), 'host_final_stop_cancelled')

    def test_later_turn_and_new_task_are_not_poisoned_by_old_stop(self):
        self.register(); self.stop()
        fresh = dict(self.task, taskId=str(uuid.uuid4()))
        self.publish(fresh); self.register(task=fresh, request='turn-2', now=1020)
        self.assertIsNone(lifecycle.stop_reason(fresh, self.config))
        self.assertEqual(lifecycle.stop_reason(self.task, self.config), 'host_final_stop_cancelled')

    def test_missing_request_id_still_distinguishes_new_explicit_task(self):
        self.register(request=''); self.stop(request='')
        self.assertIsNotNone(lifecycle.stop_reason(self.task, self.config))
        fresh = dict(self.task, taskId=str(uuid.uuid4()), lease=dict(self.task['lease'], expiresAt=1140))
        self.publish(fresh); self.register(task=fresh, request='', now=1021)
        self.assertIsNone(lifecycle.stop_reason(fresh, self.config))

    def test_session_end_only_revokes_that_session(self):
        self.register(); self.stop(event='SessionEnd', request='')
        self.assertEqual(lifecycle.stop_reason(self.task, self.config), 'host_session_end')

    def test_session_end_before_first_receipt_cannot_revive_protection(self):
        self.stop(event='SessionEnd')
        self.register(now=1020)
        self.assertEqual(lifecycle.stop_reason(self.task, self.config), 'host_session_end')

    def test_all_phases_remain_associated_after_phase_end(self):
        self.register()
        first = self.task
        fresh = dict(first, taskId=str(uuid.uuid4()))
        self.publish(fresh); self.register(task=fresh, now=1005)
        # The task file may already contain no active interface phase.
        (self.config / 'task-windows.json').write_text('{"activeTaskId":null,"tasks":{}}')
        self.stop()
        for task in (first, fresh):
            self.assertEqual(lifecycle.stop_reason(task, self.config), 'host_final_stop_cancelled')

    def test_missing_turn_id_new_run_preserves_old_stop(self):
        self.register(request=''); self.stop(request='')
        old = self.task
        fresh = dict(old, taskId=str(uuid.uuid4()), lease=dict(old['lease'], expiresAt=1140))
        self.publish(fresh); self.register(task=fresh, request='', now=1021)
        self.assertIsNone(lifecycle.stop_reason(fresh, self.config))
        self.assertEqual(lifecycle.stop_reason(old, self.config), 'host_final_stop_cancelled')

    def test_receipt_cannot_reassign_a_phase_to_another_session(self):
        self.register()
        other = str(uuid.uuid4())
        self.register(session=other, now=1005)
        self.stop(session=other)
        self.assertIsNone(lifecycle.stop_reason(self.task, self.config))
        self.stop()
        self.assertEqual(lifecycle.stop_reason(self.task, self.config), 'host_final_stop_cancelled')

    def test_other_business_activity_only_renews_its_own_acknowledged_run(self):
        from lifecycle_store import host_binding
        self.register()
        def activity(session, tool, action=None, now=1005):
            lifecycle.handle_hook({'session_id': session, 'hook_event_name': 'PostToolUse',
                'tool_name': tool, 'tool_input': {'action': action} if action else {},
                'tool_response': 'sensitive content must not be persisted'}, self.config, 'turn-1', now)
        activity('unrelated', 'Read')
        activity(self.session, 'quietbridge_task', 'status')
        activity(self.session, 'quietbridge_permissions', 'status')
        self.assertEqual(host_binding(self.task, self.config)['lastActivityAt'], 1001)
        activity(self.session, 'Read', now=1006)
        self.assertEqual(host_binding(self.task, self.config)['lastActivityAt'], 1006)
        self.assertNotIn('sensitive content', (self.config / 'host-lifecycle/state.json').read_text())
        self.stop()
        activity(self.session, 'Read', now=1020)
        self.assertEqual(host_binding(self.task, self.config)['lastActivityAt'], 1006)

    def test_foreign_tool_failed_receipt_and_noncurrent_task_are_ignored(self):
        for name, receipt in [('Read', self.task), ('mcp__quietbridge__quietbridge_task', {'isError': True, 'content': json.dumps(self.task)}),
                              ('mcp__quietbridge__quietbridge_task', dict(self.task, taskId=str(uuid.uuid4())))]:
            lifecycle.handle_hook({'session_id': self.session, 'hook_event_name': 'PostToolUse',
                'tool_name': name, 'tool_input': {'action': 'begin'}, 'tool_response': receipt},
                self.config, 'turn-1', 1001)
        self.stop()
        self.assertIsNone(lifecycle.stop_reason(self.task, self.config))

    def test_hook_never_follows_a_symlink(self):
        outside = self.config / 'elsewhere'
        outside.mkdir()
        (self.config / 'host-lifecycle').symlink_to(outside)
        with self.assertRaises(ValueError): self.register()
        self.assertEqual(list(outside.iterdir()), [])

    def test_actual_command_receives_contextual_session_and_turn_from_host(self):
        env = dict(os.environ, CODEBUDDY_SESSION_ID=self.session,
                   CODEBUDDY_CONVERSATION_REQUEST_ID='actual-turn')
        def hook(payload):
            result = subprocess.run([sys.executable, '-B', str(Path(lifecycle.__file__)),
                '--config-dir', str(self.config)], input=json.dumps(payload), capture_output=True,
                text=True, timeout=3, env=env)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        hook({'hook_event_name': 'PostToolUse', 'session_id': 'parent-session',
              'tool_name': 'quietbridge__quietbridge_task', 'tool_input': {'action': 'bind'},
              'tool_response': {'content': json.dumps(self.task)}})
        hook({'hook_event_name': 'FinalStop', 'session_id': 'parent-session', 'final_stop_reason': 'cancelled'})
        self.assertEqual(lifecycle.stop_reason(self.task, self.config), 'host_final_stop_cancelled')
        state = json.loads((self.config / 'host-lifecycle/state.json').read_text())
        turn = next(iter(state['turns'].values()))
        self.assertEqual((turn['sessionId'], turn['requestId']), (self.session, 'actual-turn'))

    def test_idle_connector_releases_on_hook_without_eof_or_next_tool(self):
        self.check_idle_hook()

    def test_idle_connector_releases_session_after_interface_phase_has_ended(self):
        self.check_idle_hook(phase_ended=True)

    def check_idle_hook(self, phase_ended=False):
        runner = '''import json,sys,time
from pathlib import Path
import server
server.TASK_FILE = str(Path(sys.argv[1])/'task-windows.json')
record={'taskId':sys.argv[2], 'state':'bound'}
server.TASK_OWNER.attach(record,Path(sys.argv[1]))
Path(server.TASK_FILE).write_text(json.dumps({'version':1,'activeTaskId':record['taskId'],'tasks':{record['taskId']:record}}))
class Helper:
 def _stop(self): Path(sys.argv[1],'released').write_text('released')
 def _alive(self): return True
server.HELPER=Helper()
if sys.argv[3]=='ended':
 server.SESSION_PROTECTION.attach(record)
 server.POWER_GUARD.update(active=True,nextRenewAt=time.monotonic()+60)
 # Register the host receipt before ending the interface phase.
 from adapters.workbuddy.lifecycle import handle_hook
 handle_hook({'session_id':sys.argv[4],'hook_event_name':'PostToolUse','tool_name':'quietbridge_task',
              'tool_input':{'action':'bind'},'tool_response':record},Path(sys.argv[1]),'turn-1')
 record['state']='ended'
 Path(server.TASK_FILE).write_text(json.dumps({'version':1,'activeTaskId':None,'tasks':{record['taskId']:record}}))
server.start_task_watch()
print(json.dumps(record),flush=True)
sys.stdin.read()
server.TASK_WATCH_STOP.set()
server.TASK_OWNER.close()
'''
        process = subprocess.Popen([sys.executable, '-B', '-c', runner, str(self.config), self.task['taskId'],
            'ended' if phase_ended else 'bound', self.session], cwd=Path(__file__).parent,
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2])),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.task = json.loads(process.stdout.readline())
            if not phase_ended:
                self.register(now=time.time())
            self.stop(now=time.time())
            until = time.monotonic() + 2
            while not (self.config / 'released').exists() and time.monotonic() < until:
                time.sleep(0.02)
            self.assertTrue((self.config / 'released').exists())
            self.assertIsNone(process.poll(), 'transport must remain open')
            record = json.loads((self.config / 'task-windows.json').read_text())['tasks'][self.task['taskId']]
            self.assertEqual(record['state'], 'ended' if phase_ended else 'interrupted')
            if not phase_ended:
                self.assertEqual(record['interruptionReason'], 'host_final_stop_cancelled')
        finally:
            process.stdin.close()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            process.stdout.close(); process.stderr.close()


if __name__ == '__main__':
    unittest.main()
