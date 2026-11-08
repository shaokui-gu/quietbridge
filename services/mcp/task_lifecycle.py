"""Kernel-backed task ownership plus a bounded, activity-driven idle lease."""
import fcntl
import copy
import os
from pathlib import Path
import stat
import time
import uuid

IDLE_SECONDS = 120
SESSION_IDLE_SECONDS = 1800


from browser_events import lease_live


class ProtectionSession:
    """A host run survives interface task end, with a bounded idle fallback.

    No heartbeat creates activity. A verified host receipt permits a 30 minute
    gap; without that evidence the original 120 second deadline still applies.
    Both wall and monotonic bounds are checked so sleep/clock changes cannot
    indefinitely extend protection.
    """
    def __init__(self):
        self.clear()

    def clear(self):
        self.record = None
        self.activity_at = 0.0
        self.activity_uptime = 0.0
        self.host_activity_at = 0.0
        self.host_activity_uptime = 0.0
        self.host_receipt_at = 0.0

    def attach(self, record):
        if self.record is None:
            self.record = copy.deepcopy(record)
        self.touch()

    def touch(self):
        if self.record is not None:
            self.activity_at = time.time()
            self.activity_uptime = time.monotonic()

    def remaining(self, binding=None):
        if self.record is None:
            return 0.0
        if binding is not None and binding.get('stoppedAt') is not None:
            return 0.0
        now, uptime = time.time(), time.monotonic()
        acknowledged = binding is not None and binding.get('stoppedAt') is None
        limit = SESSION_IDLE_SECONDS if acknowledged else IDLE_SECONDS
        if acknowledged:
            receipt = binding.get('lastActivityAt', binding.get('updatedAt', 0))
            host_activity = min(now, receipt)
            if receipt > self.host_receipt_at:
                self.host_receipt_at = receipt
                self.host_activity_at = host_activity
                # Account for delayed receipts; reading the same timestamp is
                # never a renewal and a clock jump cannot create more time.
                self.host_activity_uptime = uptime - max(0, now - host_activity)
        wall = max(self.activity_at, self.host_activity_at if acknowledged else 0)
        monotonic = max(self.activity_uptime, self.host_activity_uptime if acknowledged else 0)
        return max(0, min(wall + limit - now, monotonic + limit - uptime))


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
