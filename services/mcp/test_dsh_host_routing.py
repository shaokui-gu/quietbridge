"""Host-environment routing for DSH per-agent connections.

DSH injects a trusted session/agent identity into the connector's environment;
a call is accepted only while that exact agent's current turn is live. The
WorkBuddy token path must stay byte-for-byte unchanged and the two modes must
never fall back into one another.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import session_routing
from session_registry import session_key
from session_routing import (HOST_NAME, ROUTE_FIELD, SessionRouter, consume,
                             host_context, host_probe, issue)


HOST_KEYS = ('QUIETBRIDGE_HOST', 'QUIETBRIDGE_HOST_SESSION_ID', 'QUIETBRIDGE_HOST_AGENT_ID')
SESSION = 'session-11111111-1111-1111-1111-111111111111'
AGENT = SESSION + '-agent'
CHILD = AGENT + '-child'


class FakeWorker:
    """Stand-in for SessionProcess so routing tests never spawn a process."""

    def __init__(self, bridge, config, key):
        self.bridge, self.config, self.key = bridge, config, key
        self.process = self
        self.context = None
        self.last_used = time.monotonic()
        self.retained = True
        self.lock = threading.Lock()

    def poll(self):
        return None

    def call(self, request, context, cancelled):
        self.context = context
        return {'key': self.key, 'sessionId': context.get('sessionId'),
                'agentId': context.get('agentId'), 'requestId': context.get('requestId'),
                'arguments': request['params']['arguments']}

    def close(self):
        pass


class HostRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name).resolve()
        saved = {key: os.environ.get(key) for key in HOST_KEYS}
        for key in HOST_KEYS:
            os.environ.pop(key, None)

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)

    def environment(self, session=SESSION, agent=AGENT, host=HOST_NAME):
        values = {key: value for key, value in zip(HOST_KEYS, (host, session, agent))
                  if value is not None}
        return mock.patch.dict(os.environ, values)

    def turn(self, request='turn-1', session=SESSION, agent=AGENT, started=1, updated=2, **fields):
        value = {'host': HOST_NAME, 'sessionId': session, 'agentId': agent,
                 'requestId': request, 'startedAt': started, 'updatedAt': updated}
        value.update(fields)
        return value

    def write_state(self, turns):
        directory = self.config / 'host-lifecycle'
        directory.mkdir(mode=0o700, exist_ok=True)
        (directory / 'state.json').write_text(json.dumps({'version': 1, 'turns': turns}))

    def call(self, name='quietbridge_click', arguments=None):
        return consume(self.config, name, {'index': 1} if arguments is None else arguments)

    # --- environment contract -------------------------------------------------

    def test_missing_host_environment_keeps_legacy_token_mode(self):
        self.assertIsNone(host_context())
        self.assertIsNone(host_probe())
        arguments = issue(self.config, 'quietbridge_click', {'index': 1},
                          'workbuddy-session', 'turn-1')
        clean, context = consume(self.config, 'quietbridge_click', dict(arguments))
        self.assertEqual(clean, {'index': 1})
        self.assertEqual(context['sessionId'], 'workbuddy-session')
        self.assertNotIn('host', context)

    def test_legacy_session_key_bytes_and_host_scoped_formula(self):
        self.assertEqual(session_key('legacy-session'),
                         hashlib.sha256(b'workbuddy\x00legacy-session').hexdigest())
        self.assertEqual(session_key(HOST_NAME, SESSION, AGENT),
                         hashlib.sha256((HOST_NAME + '\0' + SESSION + '\0' + AGENT).encode()).hexdigest())
        self.assertNotEqual(session_key(HOST_NAME, SESSION, AGENT),
                            session_key(HOST_NAME, SESSION, CHILD))
        self.assertNotEqual(session_key(HOST_NAME, SESSION, AGENT),
                            session_key(HOST_NAME, 'session-other', AGENT))
        with self.assertRaisesRegex(ValueError, 'SESSION_ROUTING_REQUIRED'):
            session_key(HOST_NAME, SESSION)

    def test_partial_or_invalid_host_identity_fails_closed(self):
        cases = [
            {'QUIETBRIDGE_HOST': HOST_NAME},
            {'QUIETBRIDGE_HOST': HOST_NAME, 'QUIETBRIDGE_HOST_SESSION_ID': SESSION},
            {'QUIETBRIDGE_HOST': HOST_NAME, 'QUIETBRIDGE_HOST_AGENT_ID': AGENT},
            {'QUIETBRIDGE_HOST': HOST_NAME, 'QUIETBRIDGE_HOST_SESSION_ID': 'bad id!',
             'QUIETBRIDGE_HOST_AGENT_ID': AGENT},
            {'QUIETBRIDGE_HOST': HOST_NAME, 'QUIETBRIDGE_HOST_SESSION_ID': SESSION,
             'QUIETBRIDGE_HOST_AGENT_ID': ''},
            {'QUIETBRIDGE_HOST': 'another-host', 'QUIETBRIDGE_HOST_SESSION_ID': SESSION,
             'QUIETBRIDGE_HOST_AGENT_ID': AGENT},
            {'QUIETBRIDGE_HOST_SESSION_ID': SESSION, 'QUIETBRIDGE_HOST_AGENT_ID': AGENT},
        ]
        for environ in cases:
            with self.subTest(environ=environ):
                with self.assertRaisesRegex(ValueError, 'HOST_ROUTING_INVALID'):
                    host_context(environ)
                self.assertIsNone(host_probe(environ))

    def test_invalid_host_environment_never_falls_back_to_tokens(self):
        arguments = issue(self.config, 'quietbridge_click', {'index': 1},
                          'workbuddy-session', 'turn-1')
        with mock.patch.dict(os.environ, {'QUIETBRIDGE_HOST': HOST_NAME}):
            with self.assertRaisesRegex(ValueError, 'HOST_ROUTING_INVALID'):
                consume(self.config, 'quietbridge_click', dict(arguments))
        # The unconsumed token is still usable in a real legacy connection.
        self.assertEqual(consume(self.config, 'quietbridge_click', dict(arguments))[0],
                         {'index': 1})

    # --- host mode call admission --------------------------------------------

    def test_host_mode_accepts_a_live_turn_without_a_call_token(self):
        self.write_state({'turn-1': self.turn()})
        with self.environment():
            clean, context = self.call()
        self.assertEqual(clean, {'index': 1})
        self.assertEqual(context, {'host': HOST_NAME, 'sessionId': SESSION,
                                   'agentId': AGENT, 'requestId': 'turn-1'})
        self.assertFalse((self.config / 'host-routing').exists())

    def test_host_mode_rejects_route_credentials_instead_of_forwarding_them(self):
        self.write_state({'turn-1': self.turn()})
        with self.environment():
            with self.assertRaisesRegex(ValueError, 'SESSION_ROUTING_INVALID'):
                self.call(arguments={'index': 1, ROUTE_FIELD: 'a' * 64})
            with self.assertRaisesRegex(ValueError, 'SESSION_ROUTING_REQUIRED'):
                consume(self.config, 'quietbridge_click', 'not-a-mapping')

    def test_missing_turn_requires_the_host_context(self):
        with self.environment(), self.assertRaisesRegex(ValueError, 'SESSION_ROUTING_REQUIRED'):
            self.call()
        self.write_state({'other-session': self.turn(session='session-other'),
                          'other-agent': self.turn(agent='agent-other')})
        with self.environment(), self.assertRaisesRegex(ValueError, 'SESSION_ROUTING_REQUIRED'):
            self.call()

    def test_stopped_turn_is_rejected_until_the_host_starts_a_new_one(self):
        self.write_state({'turn-1': self.turn(stoppedAt=5, reason='completed', updated=5)})
        with self.environment(), self.assertRaisesRegex(ValueError, 'TASK_CANCELLED'):
            self.call()
        self.write_state({'turn-1': self.turn(stoppedAt=5, updated=5),
                          'turn-2': self.turn(request='turn-2', started=6, updated=7)})
        with self.environment():
            self.assertEqual(self.call()[1]['requestId'], 'turn-2')

    def test_newest_turn_wins_with_started_at_then_updated_at(self):
        self.write_state({'old': self.turn(request='turn-old', started=1, updated=2),
                          'new': self.turn(request='turn-new', started=3, updated=4)})
        with self.environment():
            self.assertEqual(self.call()[1]['requestId'], 'turn-new')
        # Without startedAt the store still orders by updatedAt.
        self.write_state({'no-start-old': self.turn(request='turn-old', started=None, updated=5),
                          'no-start-new': self.turn(request='turn-new', started=None, updated=20)})
        with self.environment():
            self.assertEqual(self.call()[1]['requestId'], 'turn-new')
        # A newer stopped turn is never bypassed by an older active one.
        self.write_state({'no-start-old': self.turn(request='turn-old', started=None, updated=5),
                          'no-start-new': self.turn(request='turn-new', started=None, updated=20,
                                                    stoppedAt=21)})
        with self.environment(), self.assertRaisesRegex(ValueError, 'TASK_CANCELLED'):
            self.call()

    def test_turn_request_id_is_an_opaque_host_identity(self):
        opaque = SESSION + ':3'
        self.write_state({'turn-1': self.turn(request=opaque)})
        with self.environment():
            self.assertEqual(self.call()[1]['requestId'], opaque)
        # Malformed or oversized request IDs are never guessed at.
        for bad in ('turn 1', 't' * 129, '', None, 3):
            with self.subTest(request=bad):
                self.write_state({'turn-1': self.turn(request=bad)})
                with self.environment(), self.assertRaisesRegex(ValueError, 'SESSION_ROUTING_REQUIRED'):
                    self.call()

    def test_agent_disposed_rejects_every_later_call(self):
        self.write_state({
            'turn-1': self.turn(),
            'end': {'host': HOST_NAME, 'sessionId': SESSION, 'agentId': AGENT,
                    'requestId': None, 'sessionEnded': True, 'stoppedAt': 9, 'updatedAt': 9}})
        with self.environment(), self.assertRaisesRegex(ValueError, 'TASK_CANCELLED'):
            self.call()

    def test_sub_agents_are_isolated_by_agent_id(self):
        self.write_state({'parent': self.turn(), 'child': self.turn(
            request='turn-child', agent=CHILD, started=3, updated=4)})
        with self.environment():
            self.assertEqual(self.call()[1]['requestId'], 'turn-1')
        with self.environment(agent=CHILD):
            self.assertEqual(self.call()[1]['requestId'], 'turn-child')
        # One agent's stop never blocks or authorizes a sibling.
        self.write_state({'parent': self.turn(),
                          'child': self.turn(request='turn-child', agent=CHILD,
                                             started=3, updated=4, stoppedAt=5)})
        with self.environment():
            self.assertEqual(self.call()[1]['requestId'], 'turn-1')
        with self.environment(agent=CHILD), self.assertRaisesRegex(ValueError, 'TASK_CANCELLED'):
            self.call()
        # A child without any live turn cannot borrow the parent's turn.
        self.write_state({'parent': self.turn()})
        with self.environment(agent=CHILD), self.assertRaisesRegex(ValueError, 'SESSION_ROUTING_REQUIRED'):
            self.call()

    # --- SessionRouter channels ----------------------------------------------

    def router(self):
        router = SessionRouter(Path('bridge.py'), self.config)
        self.addCleanup(router.close)
        return router

    def route(self, router):
        return router.call({'id': 1, 'method': 'tools/call',
                            'params': {'name': 'quietbridge_click', 'arguments': {'index': 1}}})

    def test_host_mode_router_uses_one_channel_per_session_and_agent(self):
        self.write_state({'parent': self.turn(),
                          'child': self.turn(request='turn-child', agent=CHILD, started=3, updated=4)})
        router = self.router()
        with mock.patch.object(session_routing, 'SessionProcess', FakeWorker):
            with self.environment():
                parent = self.route(router)
            with self.environment(agent=CHILD):
                child = self.route(router)
        self.assertEqual(parent['key'], session_key(HOST_NAME, SESSION, AGENT))
        self.assertEqual(child['key'], session_key(HOST_NAME, SESSION, CHILD))
        self.assertNotEqual(parent['key'], child['key'])
        self.assertEqual(set(router.workers), {parent['key'], child['key']})
        self.assertEqual((parent['requestId'], child['requestId']), ('turn-1', 'turn-child'))

    def test_token_mode_router_keeps_the_legacy_worker_channel(self):
        arguments = issue(self.config, 'quietbridge_click', {'index': 1},
                          'workbuddy-session', 'turn-1')
        router = self.router()
        with mock.patch.object(session_routing, 'SessionProcess', FakeWorker):
            reply = router.call({'id': 2, 'method': 'tools/call',
                                 'params': {'name': 'quietbridge_click', 'arguments': arguments}})
        self.assertEqual(reply['key'],
                         hashlib.sha256(b'workbuddy\x00workbuddy-session').hexdigest())
        self.assertEqual(reply['sessionId'], 'workbuddy-session')
        self.assertEqual(reply['arguments'], {'index': 1})

    def test_real_host_worker_gets_the_host_scoped_channel(self):
        bridge = self.config / 'worker.py'
        bridge.write_text('''import json,os,sys
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')!='tools/call': continue
 print(json.dumps({'id':m['id'],'result':{'pid':os.getpid(),
  'key':os.environ['QUIETBRIDGE_SESSION_KEY'],
  'value':m['params']['arguments']}}),flush=True)
''')
        self.write_state({'parent': self.turn(),
                          'child': self.turn(request='turn-child', agent=CHILD, started=3, updated=4)})
        router = SessionRouter(bridge, self.config)
        self.addCleanup(router.close)
        with self.environment():
            parent = self.route(router)
        with self.environment(agent=CHILD):
            child = self.route(router)
        self.assertEqual(parent['key'], session_key(HOST_NAME, SESSION, AGENT))
        self.assertEqual(child['key'], session_key(HOST_NAME, SESSION, CHILD))
        self.assertNotEqual(parent['pid'], child['pid'])

    def test_fourth_host_agent_is_rejected_without_eviction(self):
        agents = (AGENT, AGENT + '-b', AGENT + '-c', AGENT + '-d')
        self.write_state({agent: self.turn(request='turn-' + agent, agent=agent,
                                           started=index + 1, updated=index + 1)
                          for index, agent in enumerate(agents)})
        router = self.router()
        with mock.patch.object(session_routing, 'SessionProcess', FakeWorker):
            with self.environment():
                for agent in agents[:3]:
                    with self.environment(agent=agent):
                        self.assertIn('key', self.route(router))
                with self.environment(agent=agents[3]):
                    rejected = self.route(router)
        self.assertEqual(rejected['structuredContent']['errorCode'], 'SESSION_CAPACITY_FULL')
        self.assertEqual(len(router.workers), 3)

    def test_one_agent_stop_reaps_only_its_own_worker(self):
        self.write_state({'parent': self.turn(),
                          'child': self.turn(request='turn-child', agent=CHILD, started=3, updated=4)})
        router = self.router()
        with mock.patch.object(session_routing, 'SessionProcess', FakeWorker):
            with self.environment():
                self.route(router)
            with self.environment(agent=CHILD):
                self.route(router)
        self.write_state({'parent': self.turn(stoppedAt=10, updated=10),
                          'child': self.turn(request='turn-child', agent=CHILD, started=3, updated=4)})
        router.reap()
        self.assertEqual(set(router.workers), {session_key(HOST_NAME, SESSION, CHILD)})

    def test_host_worker_never_writes_workbuddy_hook_receipts(self):
        import server
        context = {'host': HOST_NAME, 'sessionId': SESSION, 'agentId': AGENT,
                   'requestId': SESSION + ':3'}
        params = {'name': 'quietbridge_task', 'arguments': {'action': 'begin'},
                  '_quietbridgeContext': context}
        with mock.patch.object(server, 'SESSION_KEY', 'worker-key'), \
                mock.patch.object(server, 'ROUTING_ENABLED', False), \
                mock.patch.object(server, 'call_tool', return_value={'ok': True}), \
                mock.patch('adapters.workbuddy.lifecycle.handle_hook') as hook:
            reply = server.handle({'id': 9, 'method': 'tools/call', 'params': params})
        self.assertEqual(reply['result'], {'ok': True})
        hook.assert_not_called()
        # Token-mode workers still acknowledge the WorkBuddy turn as before.
        legacy = {'sessionId': 'workbuddy-session', 'requestId': 'turn-1'}
        with mock.patch.object(server, 'SESSION_KEY', 'worker-key'), \
                mock.patch.object(server, 'ROUTING_ENABLED', False), \
                mock.patch.object(server, 'call_tool', return_value={'ok': True}), \
                mock.patch('adapters.workbuddy.lifecycle.handle_hook') as hook:
            server.handle({'id': 10, 'method': 'tools/call',
                           'params': dict(params, _quietbridgeContext=legacy)})
        hook.assert_called_once()
        self.assertNotIn('host', hook.call_args.args[0])

    # --- connector startup ----------------------------------------------------

    def test_tools_list_hides_route_field_only_in_host_mode(self):
        import server
        self.assertIsNone(server.HOST_ROUTING)
        self.assertTrue(server.ROUTING_ENABLED)
        token_tools = server.handle({'id': 11, 'method': 'tools/list'})['result']['tools']
        self.assertTrue(token_tools)
        for tool in token_tools:
            self.assertIn(ROUTE_FIELD, json.dumps(tool['inputSchema']))
        identity = {'host': HOST_NAME, 'sessionId': SESSION, 'agentId': AGENT}
        with mock.patch.object(server, 'HOST_ROUTING', identity):
            host_tools = server.handle({'id': 12, 'method': 'tools/list'})['result']['tools']
        self.assertEqual([tool['name'] for tool in host_tools],
                         [tool['name'] for tool in token_tools])
        for tool in host_tools:
            self.assertNotIn(ROUTE_FIELD, json.dumps(tool['inputSchema']))
        # Host mode still routes calls through SessionRouter, without the field.
        fake = mock.Mock()
        fake.call.return_value = {'ok': True}
        with mock.patch.object(server, 'HOST_ROUTING', identity), \
                mock.patch.object(server, 'SESSION_ROUTER', fake):
            reply = server.handle({'id': 13, 'method': 'tools/call',
                                   'params': {'name': 'quietbridge_click',
                                              'arguments': {'index': 1}}})
        self.assertEqual(reply['result'], {'ok': True})
        fake.call.assert_called_once()

    def test_startup_resolves_host_identity_and_never_routes_workers(self):
        import server
        identity = {'host': HOST_NAME, 'sessionId': SESSION, 'agentId': AGENT}
        environ = {'QUIETBRIDGE_HOST': HOST_NAME,
                   'QUIETBRIDGE_HOST_SESSION_ID': SESSION,
                   'QUIETBRIDGE_HOST_AGENT_ID': AGENT}
        self.assertEqual(server.startup_host_routing('', environ), identity)
        # A per-session worker is trusted by its key and executes directly.
        self.assertIsNone(server.startup_host_routing('worker-key', environ))
        with self.assertRaisesRegex(ValueError, 'HOST_ROUTING_INVALID'):
            server.startup_host_routing('', {'QUIETBRIDGE_HOST': HOST_NAME})
        with self.assertRaisesRegex(ValueError, 'HOST_ROUTING_INVALID'):
            server.startup_host_routing('', dict(environ, QUIETBRIDGE_HOST_AGENT_ID='bad id!'))
        self.assertIsNone(server.HOST_ROUTING)
        self.assertTrue(server.ROUTING_ENABLED)


if __name__ == '__main__':
    unittest.main()
