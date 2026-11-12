"""Bounded webpage-preview mailbox. Frames never enter command verification."""
import copy
import re
import time

IDENTITY = ('taskId', 'bindingId', 'assignmentVersion', 'browserWindowId', 'tabId', 'documentId', 'navigationEpoch')


def identity(record):
    if not record or record.get('paused'):
        return None
    event = record.get('event') or {}
    if not event.get('documentId') or not event.get('domReady') or event.get('phase') in ('before', 'paused', 'error') or event.get('readyState') not in ('interactive', 'complete'):
        return None
    return {k: (event.get(k) if k in ('documentId', 'navigationEpoch') else record.get(k)) for k in IDENTITY}


def sync_preview(peer, record, message, now):
    lease = peer.get('previewLease')
    current = identity(record)
    if (not lease or not current or not message['enabled'] or not message['permission'] or
            lease.get('expiresAt', 0) <= now or any(lease.get(k) != current[k] for k in IDENTITY)):
        peer.pop('previewFrame', None)
        return None
    frame = message.get('previewFrame')
    if (type(frame) is dict and set(frame) == {'leaseId', 'identity', 'data', 'capturedAt'} and
            frame['leaseId'] == lease['leaseId'] and frame['identity'] == current and
            type(frame['capturedAt']) in (int, float) and now - 3 <= frame['capturedAt'] <= now and
            type(frame['data']) is str and 0 < len(frame['data']) <= 180000 and
            re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', frame['data'])):
        peer['previewFrame'] = copy.deepcopy(frame)
    return copy.deepcopy(lease)


class BrowserPreview:
    """One session's leased producer; caller serializes tick/start/stop with tool IO."""
    def __init__(self, task_file, event_file, request, connection):
        self.task_file, self.event_file = task_file, event_file
        self.request, self.connection = request, connection
        self.lease = None
        self.sequence = 0
        self.document = None
        self.deadline = 0
        self.owner_connection = None

    def start(self, task_id, app, window, duration):
        import browser_events as events
        import secrets
        with events.event_store(self.event_file) as data:
            record = events.current_assignment(data, self.task_file)
            peer = data.setdefault('peer', {})
            events.require(record and record['taskId'] == task_id and record.get('mappingVerified') and
                           record.get('mapping', {}).get('windowId') == window and
                           'browser_preview' in peer.get('capabilities', []) and peer.get('enabled') and
                           peer.get('permission') and time.time() - peer.get('lastSeen', 0) < 5,
                           'BROWSER_PREVIEW_UNAVAILABLE: 网页预览需要新版扩展与有效目标映射')
            binding = record['bindingId']
        if self.lease and self.lease['bindingId'] == binding and self.owner_connection == self.connection():
            self.request('preview', {'action': 'browserRenew', 'leaseId': self.lease['leaseId'], 'durationSeconds': duration})
            self.deadline = time.monotonic() + duration
            return
        self.stop()
        lease_id = secrets.token_hex(16)
        self.lease = {'leaseId': lease_id, 'taskId': task_id, 'bindingId': binding, 'windowId': window}
        self.owner_connection = self.connection()
        self.deadline = time.monotonic() + duration
        self.document = None
        self.sequence = 0
        try:
            self.request('preview', {'action': 'hold', 'appHandle': app, 'windowId': window,
                                    'source': 'browser_content', 'leaseId': lease_id,
                                    'durationSeconds': duration, 'waitForFrame': False})
            self.tick()
        except Exception:
            try:
                self.stop()
            except Exception:
                pass
            raise

    def stop(self):
        import browser_events as events
        previous = self.lease
        self.lease = None
        self.document = None
        try:
            with events.event_store(self.event_file) as data:
                data.setdefault('peer', {}).pop('previewLease', None)
                data['peer'].pop('previewFrame', None)
        finally:
            if previous and self.owner_connection == self.connection():
                self.request('preview', {'action': 'release'})

    def tick(self):
        if not self.lease:
            return
        import browser_events as events
        now = time.time()
        invalid = False
        frame = None
        current = None
        with events.event_store(self.event_file) as data:
            record = events.current_assignment(data, self.task_file)
            peer = data.setdefault('peer', {})
            invalid = (time.monotonic() >= self.deadline or self.owner_connection != self.connection() or
                       not record or record.get('bindingId') != self.lease['bindingId'] or
                       record.get('taskId') != self.lease['taskId'] or record.get('paused') or
                       not record.get('mappingVerified') or
                       record.get('mapping', {}).get('windowId') != self.lease['windowId'] or
                       not peer.get('enabled') or not peer.get('permission') or
                       now - peer.get('lastSeen', 0) >= 5 or
                       peer.get('applicationSession') != events.application_session())
            if not invalid:
                current = identity(record)
                frame = peer.pop('previewFrame', None)
                if current:
                    peer['previewLease'] = {**current, 'leaseId': self.lease['leaseId'], 'expiresAt': now + 3}
                else:
                    peer.pop('previewLease', None)
        if invalid:
            self.stop()
            return
        # Clear outgoing document immediately, including navigation's unready interval.
        document = repr(current)
        if document != self.document:
            self.document = document
            self.sequence += 1
            self.request('preview', {'action': 'browserFrame', 'leaseId': self.lease['leaseId'],
                                    'documentKey': document, 'sequence': self.sequence})
        if (frame and current and frame.get('leaseId') == self.lease['leaseId'] and
                frame.get('identity') == current and now - 3 <= frame.get('capturedAt', 0) <= now):
            self.sequence += 1
            self.request('preview', {'action': 'browserFrame', 'leaseId': self.lease['leaseId'],
                                    'documentKey': document, 'sequence': self.sequence, 'data': frame['data'],
                                    'capturedAt': frame['capturedAt']})
