import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('stable_connector', Path(__file__).with_name('connector.py'))
connector_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(connector_module)

FAKE_BRIDGE = '''import json, time
from pathlib import Path
class Helper:
    def _stop(self): pass
HELPER = Helper()
def handle(message):
    if message.get('method', '').startswith('notifications/'): return None
    if message.get('method') == 'initialize':
        return {'id': message['id'], 'result': {'capabilities': {'tools': {}}, 'serverInfo': {'version': VERSION}}}
    if message.get('method') == 'tools/call':
        with Path(__file__).with_name('calls').open('a') as stream: stream.write('input\\n')
        time.sleep(0.4)
    return {'id': message['id'], 'result': {'version': VERSION}}
'''


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.entry = self.root / 'connector/connector.py'
        self.entry.parent.mkdir()
        self.entry.write_bytes(Path(__file__).with_name('connector.py').read_bytes())
        self.entry.with_name('compatibility.py').write_bytes(Path(__file__).with_name('compatibility.py').read_bytes())
        self.entry.with_name('stdio_transport.py').write_bytes(Path(__file__).with_name('stdio_transport.py').read_bytes())
        self.manifest = self.entry.with_name('active.json')
        self.releases = []
        for version in (1, 2):
            path = self.root / ('releases/v%s/server.py' % version)
            path.parent.mkdir(parents=True)
            path.write_text('VERSION = %s\n' % version + FAKE_BRIDGE)
            self.releases.append(path)
        self.publish(1)

    def publish(self, version, bridge=None):
        value = {'generation': str(version), 'bridge': str(bridge or self.releases[version - 1]),
                 'helper': '/not-launched', 'version': str(version)}
        temporary = self.manifest.with_suffix('.next')
        temporary.write_text(json.dumps(value))
        temporary.replace(self.manifest)

    def start(self):
        self.proc = subprocess.Popen([sys.executable, '-B', str(self.entry)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def close():
            if self.proc.poll() is None:
                self.proc.stdin.close()
                try: self.proc.wait(timeout=4)
                except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait()
            self.proc.stdout.close()
            self.proc.stderr.close()
        self.addCleanup(close)
        self.replies = queue.Queue()
        def reader():
            for line in self.proc.stdout:
                self.replies.put(json.loads(line))
        threading.Thread(target=reader, daemon=True).start()
        self.send(1, 'initialize')
        initialized = self.reply()
        self.assertTrue(initialized['result']['capabilities']['tools']['listChanged'])
        self.send(None, 'notifications/initialized')

    def send(self, identifier, method):
        self.proc.stdin.write(json.dumps({'id': identifier, 'method': method}) + '\n')
        self.proc.stdin.flush()

    def reply(self):
        return self.replies.get(timeout=4)

    def test_live_upgrade_keeps_stdio_and_never_replays_in_flight_input(self):
        self.start()
        original_pid = self.proc.pid
        self.send(2, 'tools/call')
        until = time.monotonic() + 2
        calls = self.releases[0].with_name('calls')
        while not calls.exists() and time.monotonic() < until:
            time.sleep(0.01)
        self.assertTrue(calls.exists())
        self.publish(2)
        self.assertEqual(self.reply(), {'id': 2, 'result': {'version': 1}})
        self.assertEqual(self.reply()['method'], 'notifications/tools/list_changed')
        self.send(3, 'tools/call')
        blocked = self.reply()['result']
        self.assertEqual(blocked['structuredContent']['errorCode'], 'CONNECTOR_UPDATED')
        self.assertEqual(blocked['structuredContent']['execution']['status'], 'not_dispatched')
        self.send(4, 'tools/list')
        self.assertEqual(self.reply()['result']['version'], 2)
        self.send(5, 'tools/call')
        self.assertEqual(self.reply()['result']['version'], 2)
        self.assertEqual(calls.read_text(), 'input\n')
        self.assertEqual(self.releases[1].with_name('calls').read_text(), 'input\n')
        self.assertEqual(self.proc.pid, original_pid)
        self.assertEqual(json.loads(self.entry.with_name('status.json').read_text())['generation'], '2')

    def test_bad_release_keeps_healthy_bridge_and_accepts_rollback(self):
        sent = []
        instance = connector_module.Connector(self.manifest, sent.append)
        with patch.dict(os.environ):
            instance.refresh()
            self.releases[1].write_text('raise RuntimeError("broken release")')
            self.publish(2)
            instance.refresh()
            self.assertEqual(instance.generation, '1')
            self.assertEqual(instance.handle({'id': 2, 'method': 'ping'})['result']['version'], 1)
            self.assertEqual(json.loads(self.entry.with_name('status.json').read_text())['state'], 'update_failed')
            self.publish(1)
            instance.refresh()
            self.assertEqual(instance.generation, '1')
            instance.close()
        self.assertEqual(sent, [])

    def test_same_release_reinstall_refreshes_without_configuration_change(self):
        self.start()
        value = json.loads(self.manifest.read_text())
        value['generation'] = 'reinstall'
        self.manifest.write_text(json.dumps(value))
        self.assertEqual(self.reply()['method'], 'notifications/tools/list_changed')
        self.assertIsNone(self.proc.poll())

    def test_symlinks_and_other_locations_are_not_loaded(self):
        instance = connector_module.Connector(self.manifest, lambda _: None)
        for path in (self.root / 'foreign.py', self.root / 'releases/v2/server.py'):
            if path.name == 'foreign.py': path.write_text(FAKE_BRIDGE)
            else:
                path.unlink()
                path.symlink_to(self.releases[0])
            self.publish(2, path)
            with self.assertRaises(ValueError): instance.refresh()
