"""Real helper regression using an isolated session; never request TCC consent."""
import fcntl
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import tempfile
import time
import unittest
import uuid


@unittest.skipUnless(os.environ.get('CUA_PERMISSION_SMOKE_EXECUTABLE'),
                     'Set CUA_PERMISSION_SMOKE_EXECUTABLE to a built native helper')
class PermissionServiceTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('CUA_LEGACY_SMOKE_EXECUTABLE'), 'Set a legacy helper to verify the upgrade barrier')
    def test_legacy_helper_cannot_rejoin_the_new_application_session(self):
        executable = os.environ['CUA_LEGACY_SMOKE_EXECUTABLE']
        with tempfile.TemporaryDirectory(prefix='cl-', dir='/tmp') as temporary:
            os.chmod(temporary, 0o700)
            lock = os.open(str(Path(temporary) / 'application.lock'), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(lock, json.dumps({'version': 2, 'sessionId': str(uuid.uuid4())}).encode())
            try:
                reply = subprocess.run([executable, '--serve', '--runtime-key', 'b' * 64,
                    '--runtime-directory', temporary, '--startup-timeout-ms', '1000'],
                    capture_output=True, timeout=3)
                self.assertNotEqual(reply.returncode, 0)
                self.assertEqual(json.loads(reply.stdout)['code'], 'APP_NOT_RUNNING')
                self.assertFalse(list(Path(temporary).glob('*.sock')))
            finally:
                os.close(lock)

    def test_watchdog_survives_preflight_and_exits_when_application_exits(self):
        executable = str(Path(os.environ['CUA_PERMISSION_SMOKE_EXECUTABLE']).resolve())
        # Keep AF_UNIX paths within macOS's 104-byte limit and avoid the user's
        # running application, its lock, its sockets and its capability choices.
        with tempfile.TemporaryDirectory(prefix='cp-', dir='/tmp') as temporary:
            os.chmod(temporary, 0o700)
            lock = os.open(str(Path(temporary) / 'application.lock'), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(lock, json.dumps({'version': 2, 'sessionId': str(uuid.uuid4())}).encode())
            key = 'a' * 64
            options = ['--runtime-key', key, '--runtime-directory', temporary,
                       '--startup-timeout-ms', '3000']
            server = subprocess.Popen([executable, '--serve', *options],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(server.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(5), 'helper did not publish readiness')
                line = server.stdout.readline()
                self.assertTrue(line, 'helper exited before readiness')
                ready = json.loads(line)
                self.assertEqual(ready.get('type'), 'ready', ready)
                self.assertEqual(ready['pid'], server.pid)
                # The launcher must find the same session in its runtime
                # directory and reuse the live server, rather than spawn again.
                launcher = subprocess.run([executable, '--ensure-shared', *options],
                                          capture_output=True, timeout=5, check=True)
                self.assertEqual(json.loads(launcher.stdout)['pid'], server.pid)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                    channel.settimeout(5)
                    channel.connect(str(Path(temporary) / (key + '.sock')))
                    with channel.makefile('rwb', buffering=0) as stream:
                        def exchange(frame):
                            stream.write(json.dumps(frame).encode() + b'\n')
                            return json.loads(stream.readline())

                        hello = exchange({'type': 'hello', 'protocol': 1, 'runtimeKey': key,
                                          'config': {'maxRequestBytes': 1048576, 'maxResponseBytes': 33554432,
                                                     'axTimeoutMs': 30000, 'textEventIntervalMs': 0.25,
                                                     'maxClickHoldMs': 5000, 'screenshotTimeoutMs': 5000,
                                                     'timeoutMs': 3000, 'graceMs': 1000}})
                        self.assertEqual(hello.get('type'), 'ready', hello)
                        for index in range(2):
                            if index:
                                time.sleep(0.35)  # Multiple real watchdog firings.
                            reply = exchange({'id': str(index), 'method': 'permissionStatus', 'params': {}})
                            self.assertNotIn('error', reply, reply)
                            status = reply['result']
                            self.assertIsInstance(status['accessibility'], bool)
                            self.assertIsInstance(status['screenRecording'], bool)
                            self.assertEqual(status['helper']['pid'], server.pid)
                        self.assertIsNone(server.poll(), 'session watchdog crashed the helper')
                        fcntl.flock(lock, fcntl.LOCK_UN)
                        os.close(lock)
                        lock = None
                        self.assertEqual(server.wait(timeout=5), 0,
                                         'helper must exit after the application session is revoked')
            finally:
                if lock is not None:
                    os.close(lock)
                if server.poll() is None:
                    server.terminate()
                _, errors = server.communicate(timeout=5)
                server.stdout.close()
                server.stderr.close()
                if server.returncode:
                    self.fail(f'helper exited with {server.returncode}: {errors.decode(errors="replace")}')


if __name__ == '__main__':
    unittest.main()
