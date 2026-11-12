import copy
from contextlib import contextmanager
import time
import unittest
from unittest import mock

import browser_events as events
from browser_preview import BrowserPreview, identity, sync_preview


class BrowserPreviewTests(unittest.TestCase):
    def setUp(self):
        self.record = dict(taskId='task', bindingId='binding', assignmentVersion=1,
                           browserWindowId=2, tabId=7, mappingVerified=True, mapping={'windowId': 9},
                           event=dict(documentId='doc', navigationEpoch=1, domReady=True, readyState='complete'))
        self.data = {'peer': dict(enabled=True, permission=True, lastSeen=time.time(),
                                 applicationSession='app', capabilities=['browser_preview'])}
        self.connection = 'connection'
        self.request = mock.Mock(return_value={})
        @contextmanager
        def store(_):
            yield self.data
        for name, replacement in [('event_store', store), ('current_assignment', lambda *_: self.record),
                                  ('application_session', lambda: 'app')]:
            patch = mock.patch.object(events, name, replacement)
            patch.start()
            self.addCleanup(patch.stop)
        self.preview = BrowserPreview('task-file', 'event-file', self.request, lambda: self.connection)

    def start(self):
        self.preview.start('task', 'app', 9, 60)
        return copy.deepcopy(self.data['peer']['previewLease'])

    def frame(self, lease):
        return dict(leaseId=lease['leaseId'], identity=identity(self.record), data='ZmFrZQ==', capturedAt=time.time())

    def test_navigation_clears_old_document_and_late_frame_does_not_verify_actions(self):
        lease = self.start()
        frame = self.frame(lease)
        self.data['peer']['pendingVerify'] = {'operation': 'click'}
        self.record['event']['documentId'] = None
        self.data['peer']['previewFrame'] = frame
        self.request.reset_mock()
        self.preview.tick()
        clear = self.request.call_args.args[1]
        self.assertNotIn('data', clear)
        self.assertNotIn('previewLease', self.data['peer'])
        self.record['event'].update(documentId='next', navigationEpoch=2)
        self.preview.tick()
        new_lease = self.data['peer']['previewLease']
        sync_preview(self.data['peer'], self.record, dict(enabled=True, permission=True, previewFrame=frame), time.time())
        self.assertNotIn('previewFrame', self.data['peer'])
        valid = self.frame(new_lease)
        sync_preview(self.data['peer'], self.record, dict(enabled=True, permission=True, previewFrame=valid), time.time())
        self.preview.tick()
        self.assertEqual(self.request.call_args.args[1]['data'], valid['data'])
        self.assertEqual(self.data['peer']['pendingVerify'], {'operation': 'click'})

    def test_stop_and_expiry_remove_mailbox_and_do_not_resurrect(self):
        for reason in ('stop', 'expiry', 'reassignment', 'revocation', 'disconnect'):
            with self.subTest(reason=reason):
                self.record.update(bindingId='binding')
                self.data['peer'].update(permission=True, lastSeen=time.time())
                self.connection = 'connection'
                lease = self.start()
                self.data['peer']['previewFrame'] = self.frame(lease)
                if reason == 'stop': self.preview.stop()
                elif reason == 'expiry': self.preview.deadline = 0
                elif reason == 'reassignment': self.record['bindingId'] = 'other'
                elif reason == 'revocation': self.data['peer']['permission'] = False
                else: self.connection = None
                self.request.reset_mock()
                self.preview.tick()
                self.assertIsNone(self.preview.lease)
                self.assertNotIn('previewFrame', self.data['peer'])
                self.assertNotIn('previewLease', self.data['peer'])
                self.preview.tick()
                if reason == 'disconnect': self.request.assert_not_called()
                else: self.assertFalse(any(c.args[1].get('action') == 'hold' for c in self.request.call_args_list))

    def test_invalid_frames_are_bounded_and_isolated(self):
        lease = self.start()
        frame = self.frame(lease)
        for changes in ({'leaseId': 'other'}, {'capturedAt': time.time() - 10}, {'data': 'a' * 180001},
                        {'identity': {**frame['identity'], 'tabId': 8}}, {'data': '<html>'}):
            peer = copy.deepcopy(self.data['peer'])
            sync_preview(peer, self.record, dict(enabled=True, permission=True, previewFrame={**frame, **changes}), time.time())
            self.assertNotIn('previewFrame', peer)
        self.data['peer']['previewLease']['expiresAt'] = 0
        self.assertIsNone(sync_preview(self.data['peer'], self.record, dict(enabled=True, permission=True), time.time()))

    def test_start_requires_mapping_and_capability_and_does_not_rebuild_same_lease(self):
        self.record['mappingVerified'] = False
        with self.assertRaises(ValueError): self.start()
        self.request.assert_not_called()
        self.record['mappingVerified'] = True
        lease = self.start()
        self.request.reset_mock()
        self.start()
        self.assertEqual(self.data['peer']['previewLease']['leaseId'], lease['leaseId'])
        self.assertEqual(self.request.call_args.args[1]['action'], 'browserRenew')

    def test_mailbox_failure_still_releases_native_and_failed_hold_cleans_up(self):
        self.start()
        with mock.patch.object(events, 'event_store', side_effect=OSError('unavailable')):
            with self.assertRaises(OSError):
                self.preview.stop()
        self.assertIsNone(self.preview.lease)
        self.assertEqual(self.request.call_args.args[1]['action'], 'release')
        def failed_hold(_method, params):
            if params['action'] == 'hold':
                raise TimeoutError('reply lost')
        self.request.side_effect = failed_hold
        with self.assertRaises(TimeoutError):
            self.start()
        self.assertIsNone(self.preview.lease)
        self.assertEqual(self.request.call_args.args[1]['action'], 'release')

    def test_candidate_capabilities_are_visible_only_for_live_authorized_session(self):
        peer = self.data['peer']
        self.assertIn('browser_preview', events.candidate_response(peer, 'app')['capabilities'])
        self.assertEqual(events.candidate_response(peer, 'other')['capabilities'], [])
        peer['permission'] = False
        self.assertEqual(events.candidate_response(peer, 'app')['capabilities'], [])
