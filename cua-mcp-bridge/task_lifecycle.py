"""Kernel-backed task ownership plus a bounded, activity-driven idle lease."""
import fcntl
import os
from pathlib import Path
import stat
import time
import uuid

IDLE_SECONDS = 120


from browser_events import lease_live


class TaskOwner:
    def __init__(self):
        self.id = str(uuid.uuid4())
        self.fd = None
        self.path = None

    def attach(self, record, parent):
        if self.path is not None and self.path.parent != Path(parent):
            self.close()
        if self.fd is None:
            self.path = Path(parent) / ('task-owner-' + self.id + '.lock')
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.write(self.fd, self.id.encode('ascii'))
            except BaseException:
                self.close()
                raise
        self.renew(record)

    def owns(self, record):
        return (record.get('lease') or {}).get('ownerId') == self.id

    def expired(self, record):
        lease = record.get('lease') or {}
        return (self.owns(record) and
                (time.monotonic() >= lease.get('expiresAtUptime', 0) or
                 time.time() >= lease.get('expiresAt', 0)))

    def renew(self, record):
        record['lease'] = {'ownerId': self.id, 'expiresAt': time.time() + IDLE_SECONDS,
                           'expiresAtUptime': time.monotonic() + IDLE_SECONDS,
                           'idleSeconds': IDLE_SECONDS}

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.path = None
