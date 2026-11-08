"""Opt-in real native regression: synthetic windows only, no input or consent."""
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import uuid


@unittest.skipUnless(os.environ.get('QUIETBRIDGE_PARALLEL_SMOKE_EXECUTABLE'), 'Requires a signed diagnostic helper')
class ParallelServiceTests(unittest.TestCase):
    def test_three_real_clients_keep_streams_and_handles_after_one_worker_dies(self):
        exe = os.environ['QUIETBRIDGE_PARALLEL_SMOKE_EXECUTABLE']
        with tempfile.TemporaryDirectory(prefix='qb3-', dir='/private/tmp') as temp:
            os.chmod(temp, 0o700)
            lock = os.open(Path(temp) / 'application.lock', os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(lock, json.dumps({'version': 2, 'sessionId': str(uuid.uuid4())}).encode())
            key = 'c' * 64
            server = subprocess.Popen([exe, '--serve', '--runtime-key', key,
                '--runtime-directory', temp, '--startup-timeout-ms', '5000'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(lambda: self.close_process(server))
            probe = subprocess.Popen([exe, '--probe-stream', 'synthetic-three', '30'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(lambda: self.close_process(probe))
            try:
                self.assertTrue(select.select([server.stdout], [], [], 8)[0], 'server startup timed out')
                ready = json.loads(server.stdout.readline())
                self.assertEqual(ready.get('type'), 'ready', ready)
                self.assertTrue(select.select([probe.stdout], [], [], 8)[0], 'synthetic probe startup timed out')
                windows = json.loads(probe.stdout.readline())
                self.assertEqual(windows.get('event'), 'syntheticWindows', windows)
                clients, handles, worker_pids, baselines = [], [], [], []
                for index, window in enumerate(windows['windowIds']):
                    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    channel.settimeout(8)
                    channel.connect(str(Path(temp) / (key + '.sock')))
                    self.addCleanup(channel.close)
                    stream = channel.makefile('rwb', buffering=0)
                    self.addCleanup(stream.close)
                    def exchange(frame, stream=stream):
                        stream.write(json.dumps(frame).encode() + b'\n')
                        while True:
                            reply = json.loads(stream.readline())
                            if reply.get('type') != 'diagnostic':
                                return reply
                    def call(method, params=None, exchange=exchange):
                        return exchange({'id': str(uuid.uuid4()), 'method': method, 'params': params or {}})
                    hello = exchange({'type': 'hello', 'protocol': 1, 'runtimeKey': key,
                        'config': {'maxRequestBytes': 1048576, 'maxResponseBytes': 33554432,
                            'axTimeoutMs': 3000, 'textEventIntervalMs': 0.25, 'maxClickHoldMs': 5000,
                            'screenshotTimeoutMs': 5000, 'timeoutMs': 5000, 'graceMs': 1000}})
                    self.assertEqual(hello.get('type'), 'ready', hello)
                    before = self.children(server.pid)
                    app = call('getApp', {'selector': str(windows['pid'])})
                    self.assertNotIn('error', app, app)
                    handle = app['result']['appHandle']
                    after = self.children(server.pid)
                    added = after - before
                    self.assertEqual(len(added), 1, (before, after))
                    worker_pids.append(added.pop())
                    shared = call('preview', {'action': 'hold', 'appHandle': handle, 'windowId': window,
                        'source': 'window', 'durationSeconds': 60})
                    self.assertNotIn('error', shared, shared)
                    self.assertTrue(shared['result']['preview']['streamReady'], shared)
                    clients.append(call); handles.append(handle)
                    baselines.append(shared['result']['preview'])
                os.kill(worker_pids[0], signal.SIGKILL)
                failed = clients[0]('listWindows', {'appHandle': handles[0]})
                self.assertEqual(failed['error']['code'], 'NATIVE_WORKER_EXITED', failed)
                time.sleep(0.8)
                report = []
                for index in (1, 2):
                    live = clients[index]('listWindows', {'appHandle': handles[index]})
                    self.assertNotIn('error', live, live)
                    state = clients[index]('preview', {'action': 'status'})['result']['preview']
                    self.assertTrue(state['streamReady'], state)
                    self.assertGreater(state['videoFrames'], baselines[index]['videoFrames'])
                    self.assertEqual(state['streamGeneration'], baselines[index]['streamGeneration'])
                    report.append({'client': index, 'videoFrames': state['videoFrames'], 'baseline': baselines[index]['videoFrames'],
                        'sameStreamGeneration': True})
                self.assertIsNone(server.poll(), 'broker exited with one worker')
                print(json.dumps({'nativeThreeClients': True, 'workerCrashIsolated': True,
                    'survivingStreams': report}), flush=True)
                for call in clients[1:]:
                    call('preview', {'action': 'release'})
            finally:
                os.close(lock)

    @staticmethod
    def children(pid):
        reply = subprocess.run(['/usr/bin/pgrep', '-P', str(pid)], capture_output=True, text=True)
        return {int(v) for v in reply.stdout.split()}

    @staticmethod
    def close_process(process):
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.communicate(timeout=2)


if __name__ == '__main__':
    unittest.main()
