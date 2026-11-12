import json
from pathlib import Path
import tempfile
import unittest

from preview_diagnostics import fixture, lifecycle_sample, load_samples, sample, summarize, load_probe_results


class PreviewDiagnosticsTests(unittest.TestCase):
    def record(self):
        return dict(event='preview.streamProgress', timestamp=100, pid=1,
                    trackingWindowId=7, streamGeneration='stream-a', targetOnCurrentSpace=False,
                    frameProgress={'contentSamples': 2, 'sampledContentChanges': 1,
                                   'submittedFrames': 10, 'lastSubmissionAgeMs': 1})

    def test_report_drops_content_and_rejects_invalid_metrics(self):
        record = self.record()
        record.update(title='private', image='private')
        record['frameProgress'].update(fingerprint='private', contentSamples=float('nan'),
                                       submittedFrames=True, sampleFailures=-1)
        result = sample(record)
        text = json.dumps(summarize([result]), allow_nan=False)
        self.assertNotIn('private', text)
        self.assertNotIn('contentSamples', result['frameProgress'])
        self.assertNotIn('submittedFrames', result['frameProgress'])
        self.assertNotIn('sampleFailures', result['frameProgress'])

    def test_streams_never_share_deltas_and_resets_are_marked(self):
        a = self.record()
        b = self.record()
        b['timestamp'] = 105
        b['frameProgress']['submittedFrames'] = 1
        c = self.record()
        c.update(timestamp=106, streamGeneration='stream-b')
        report = summarize([sample(r) for r in (a, b, c)])
        self.assertEqual(report['streamCount'], 2)
        self.assertEqual(len(report['intervals']), 1)
        self.assertTrue(report['intervals'][0]['counterReset'])
        self.assertEqual(report['intervals'][0]['delta'], {})

    def test_rotated_duplicate_partial_and_wrong_window_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'native.jsonl'
            path.write_text(json.dumps(self.record()) + '\n' + '{partial')
            self.assertEqual(len(load_samples([path, path], since=99, window_id=7)), 1)
            self.assertEqual(load_samples([path], since=101), [])
            self.assertEqual(load_samples([path], window_id=8), [])

    def test_real_deltas_do_not_claim_confirmed_presentation(self):
        a, b = self.record(), self.record()
        b.update(timestamp=105, targetOnCurrentSpace=True)
        b['frameProgress'].update(contentSamples=7, sampledContentChanges=4, submittedFrames=20)
        interval = summarize([sample(a), sample(b)])['intervals'][0]
        self.assertEqual(interval['delta']['sampledContentChanges'], 3)
        self.assertEqual(interval['delta']['submittedFrames'], 10)
        self.assertFalse(interval['targetOnCurrentSpaceBefore'])
        self.assertTrue(interval['targetOnCurrentSpaceAfter'])
        self.assertNotIn('displayedFrames', interval['delta'])

    def test_body_change_deltas_exclude_whole_window_only_changes(self):
        a, b = self.record(), self.record()
        a['frameProgress'].update(bodySamples=2, bodyContentChanges=0, bodyNearUniformSamples=2)
        b['timestamp'] = 105
        b['frameProgress'].update(contentSamples=7, sampledContentChanges=4,
            bodySamples=7, bodyContentChanges=0, bodyNearUniformSamples=7,
            sourceFrame={'pixelFormat': 'BGRA', 'bodyNearUniform': True,
                         'bodyAlphaMin': 255, 'bodyAlphaMax': 255, 'title': 'private-content'})
        report = summarize([sample(a), sample(b)])
        self.assertEqual(report['intervals'][0]['delta']['sampledContentChanges'], 3)
        self.assertEqual(report['intervals'][0]['delta']['bodyContentChanges'], 0)
        self.assertNotIn('private-content', json.dumps(report))

    def test_lifecycle_tracks_pending_failed_and_completed_stops_per_generation(self):
        def event(time, generation, state, suffix):
            return dict(event='preview.capture' + suffix, timestamp=time, pid=1,
                        trackingWindowId=7, streamGeneration=generation, startState='completed',
                        stopState=state, stopConfirmed=state == 'completed', message='private-content')
        rows = [event(100, 'old', 'requested', 'StopRequested'),
                event(101, 'new', 'not_requested', 'StartCompleted'),
                event(102, 'old', 'completed', 'StopCompleted'),
                event(103, 'pending', 'requested', 'StopRequested'),
                event(104, 'failed', 'failed', 'StopFailed')]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'native.jsonl'
            path.write_text('\n'.join(json.dumps(row) for row in rows + rows))
            parsed = load_samples([path, path], parser=lifecycle_sample)
        self.assertEqual(len(parsed), 5)
        report = summarize([], parsed)
        self.assertEqual(report['pendingCaptureStops'], 1)
        self.assertEqual(report['failedCaptureStops'], 1)
        self.assertEqual(len(report['captureStops']), 3)
        self.assertEqual(report['captureStops'][0]['streamGeneration'], 'old')
        self.assertTrue(report['captureStops'][0]['stopConfirmed'])
        self.assertNotIn('private-content', json.dumps(report))

    def test_invalid_generations_and_malformed_events_are_rejected(self):
        record = self.record()
        for generation in ('private-content/URL', 'x' * 65, [], 12):
            record['streamGeneration'] = generation
            self.assertIsNone(sample(record))
        for event in ([], {}, 'private-content'):
            self.assertIsNone(lifecycle_sample(dict(event=event, timestamp=100)))

    def test_render_probe_correlation_filters_content_and_deduplicates_polling(self):
        result = dict(stage='layout', startedAt=100, completedAt=101, performed=True,
            attachedBefore=True, attachedAfter=True, pageEnabledBefore=True, pageEnabledAfter=True,
            viewportWidth=1600, viewportHeight=1000)
        row = dict(event='browser.render_probe_result', timestamp=105, taskId='task',
                   commandId='a' * 32, result=result, title='private-content')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'bridge.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in [row, dict(row, timestamp=110),
                dict(row, result=dict(result, data='private-content'))]) + '\n{partial')
            parsed = load_probe_results([path, path], since=100, task_id='task')
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0]['timestamp'], 105)
            self.assertEqual(parsed[0]['result'], result)
            self.assertNotIn('private-content', json.dumps(parsed))
            self.assertEqual(load_probe_results([path], task_id='other'), [])

    def test_fixture_navigation_changes_large_static_content_without_scripts(self):
        self.assertNotEqual(fixture(0), fixture(1))
        self.assertIn(b'data-preview-step="1"', fixture(1))
        self.assertIn(b'<h1>STEP 1</h1>', fixture(1))
        self.assertIn(b'SCROLL 3', fixture(1))
        self.assertNotIn(b'<script', fixture(1))


if __name__ == '__main__':
    unittest.main()
