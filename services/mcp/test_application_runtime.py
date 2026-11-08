import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

os.environ.setdefault('QUIETBRIDGE_CONFIG_DIR', tempfile.mkdtemp(prefix='quietbridge-test-config-'))

import browser_events as events
import server


class ApplicationRuntimeTests(unittest.TestCase):
    def test_exit_and_restart_revoke_the_previous_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary).resolve()
            runtime.chmod(0o700)
            lock = runtime / 'application.lock'
            first, second = str(uuid.uuid4()), str(uuid.uuid4())
            lock.write_text(json.dumps({'version': 2, 'sessionId': first}))
            lock.chmod(0o600)
            self.assertIsNone(events.application_session(runtime))
            with lock.open('r+') as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(events.application_session(runtime), first)
                lock.chmod(0o644)
                self.assertIsNone(events.application_session(runtime))
                lock.chmod(0o600)
            self.assertIsNone(events.application_session(runtime))
            with lock.open('r+') as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                owner.write(json.dumps({'version': 2, 'sessionId': second}))
                owner.truncate()
                owner.flush()
                self.assertEqual(events.application_session(runtime), second)
                alias = runtime / 'alias'
                alias.symlink_to(runtime, target_is_directory=True)
                self.assertIsNone(events.application_session(alias))

    def test_control_endpoint_must_belong_to_the_live_session(self):
        for reply, expected in (({'master': True}, None),
                                ({'master': True, '_service': {'applicationSession': 'old'}}, None),
                                ({'master': True, '_service': {'applicationSession': 'current'}}, True)):
            with self.subTest(reply=reply), mock.patch.object(server, 'application_session', return_value='current'), \
                    mock.patch.object(server.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(reply).encode())):
                result = server.control_snapshot()
                self.assertEqual(result.get('master') if result else None, expected)
        with mock.patch.object(server, 'application_session', return_value=None), \
                mock.patch.object(server.urllib.request, 'urlopen') as http:
            self.assertIsNone(server.control_snapshot())
            http.assert_not_called()

    def test_legacy_or_unknown_session_protocol_cannot_enable_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary).resolve()
            runtime.chmod(0o700)
            lock = runtime / 'application.lock'
            lock.touch(mode=0o600)
            with lock.open('w+') as owner:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for value in (str(uuid.uuid4()), {'version': 1, 'sessionId': str(uuid.uuid4())},
                              {'version': True, 'sessionId': str(uuid.uuid4())}):
                    owner.seek(0)
                    owner.write(value if isinstance(value, str) else json.dumps(value))
                    owner.truncate()
                    owner.flush()
                    self.assertIsNone(events.application_session(runtime))
