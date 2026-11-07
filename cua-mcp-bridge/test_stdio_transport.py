import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

RUNNER = '''from stdio_transport import serve
import json, sys
from pathlib import Path

def send(value): print(json.dumps(value), flush=True)
def dispatch(message, token):
    if message.get('method') != 'tools/call':
        return {'id': message.get('id'), 'result': {}}
    if token.is_set():
        return {'id': message['id'], 'cancelled': True, 'started': False}
    send({'started': message['id']})
    if message.get('params', {}).get('name') == 'wait':
        token.wait(4)
    return {'id': message['id'], 'cancelled': token.is_set()}
serve(dispatch, send, on_close=lambda: Path(sys.argv[1]).write_text('closed'))
'''


class StdioTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.marker = Path(temporary.name) / 'closed'
        self.proc = subprocess.Popen([sys.executable, '-B', '-c', RUNNER, str(self.marker)],
            cwd=Path(__file__).parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.output = queue.Queue()
        def read():
            for line in self.proc.stdout:
                self.output.put(json.loads(line))
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        def close():
            if self.proc.poll() is None:
                if not self.proc.stdin.closed: self.proc.stdin.close()
                try: self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait()
            self.reader.join(timeout=2)
            for stream in (self.proc.stdout, self.proc.stderr): stream.close()
        self.addCleanup(close)

    def send(self, value):
        self.proc.stdin.write(json.dumps(value) + '\n')
        self.proc.stdin.flush()

    def call(self, identifier, name='wait'):
        self.send({'jsonrpc': '2.0', 'id': identifier, 'method': 'tools/call', 'params': {'name': name}})

    def cancel(self, identifier):
        self.send({'method': 'notifications/cancelled', 'params': {'requestId': identifier}})

    def next(self):
        return self.output.get(timeout=2)

    def test_cancel_interrupts_inflight_request_and_queue_does_not_replay(self):
        self.call(1)
        self.assertEqual(self.next(), {'started': 1})
        self.call(2)
        self.cancel(2)
        self.cancel(1)
        self.assertEqual(self.next(), {'id': 1, 'cancelled': True})
        self.assertEqual(self.next(), {'id': 2, 'cancelled': True, 'started': False})
        self.call(3, 'instant')
        self.assertEqual(self.next(), {'started': 3})
        self.assertEqual(self.next(), {'id': 3, 'cancelled': False})

    def test_eof_cancels_inflight_and_abandons_queued_input(self):
        self.call(1)
        self.assertEqual(self.next(), {'started': 1})
        self.call(2, 'instant')
        self.proc.stdin.close()
        self.assertEqual(self.next(), {'id': 1, 'cancelled': True})
        self.proc.wait(timeout=2)
        self.reader.join(timeout=1)
        self.assertTrue(self.output.empty())
        self.assertEqual(self.marker.read_text(), 'closed')

    def test_cancel_ids_are_typed_and_unknown_or_late_cancel_is_ignored(self):
        self.cancel(1)  # No request yet: cannot poison a later call.
        self.call(1)
        self.assertEqual(self.next(), {'started': 1})
        self.cancel(True)
        self.cancel('1')
        self.send({'method': 'notifications/cancelled', 'params': ['bad']})
        with self.assertRaises(queue.Empty): self.output.get(timeout=0.15)
        self.cancel(1)
        self.assertTrue(self.next()['cancelled'])
        self.cancel(1)  # Completed ID can be reused without old cancellation.
        self.call(1, 'instant')
        self.assertEqual(self.next(), {'started': 1})
        self.assertFalse(self.next()['cancelled'])

    def test_invalid_params_and_missing_call_id_do_not_dispatch(self):
        self.send({'id': 1, 'method': 'tools/call', 'params': []})
        self.assertEqual(self.next()['error']['code'], -32600)
        self.send({'method': 'tools/call', 'params': {'name': 'instant'}})
        self.assertEqual(self.next()['error']['code'], -32600)
        self.call('ok', 'instant')
        self.assertEqual(self.next(), {'started': 'ok'})
        self.assertFalse(self.next()['cancelled'])
