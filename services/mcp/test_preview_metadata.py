"""Content-free metadata boundary, tested without capture or user state."""
import json
import unittest

from preview_metadata import capture_lifecycle, frame_progress, source_frame


class PreviewMetadataTests(unittest.TestCase):
    def test_malformed_values_never_become_diagnostics(self):
        for value in (None, [], 'private-content', 1, True):
            self.assertEqual(source_frame(value), {})
            self.assertEqual(frame_progress(value), {})
            self.assertEqual(capture_lifecycle(value), {})
        for value in ([], {}, True, -1, float('nan'), float('inf'), 'private-content'):
            with self.subTest(value=value):
                self.assertEqual(frame_progress({'bodySamples': value}), {})
                self.assertEqual(source_frame({'width': value, 'colorPrimaries': value}), {})
                self.assertEqual(capture_lifecycle({'trackingWindowId': value, 'startElapsedMs': value}), {})

    def test_source_metadata_uses_known_identifiers_and_bounded_numbers_only(self):
        value = {'pixelFormat': 'BGRA', 'pixelFormatCode': 1111970369,
                 'width': 1920, 'height': 1080, 'planeCount': 0, 'bodyX': 240,
                 'bodyAlphaMin': 0, 'bodyAlphaMax': 255, 'bodyChannelSpread': 256,
                 'bodyNearUniform': False, 'colorPrimaries': 'P3_D65',
                 'transferFunction': 'private-content', 'yCbCrMatrix': [],
                 'fingerprint': 'private-content', 'pixels': 'private-content'}
        result = source_frame(value)
        self.assertEqual(result['bodyAlphaMax'], 255)
        self.assertFalse(result['bodyNearUniform'])
        self.assertNotIn('bodyChannelSpread', result)
        self.assertNotIn('private-content', json.dumps(result, allow_nan=False))
        self.assertNotIn('yCbCrMatrix', result)

    def test_stop_confirmation_requires_completed_state_and_original_generation(self):
        for state in ('not_requested', 'requested', 'failed', 'completed', [], 'private-content'):
            result = capture_lifecycle({'streamGeneration': 'old-stream', 'trackingWindowId': 7,
                                        'stopState': state, 'stopConfirmed': True,
                                        'platformErrorDomain': 'other', 'platformErrorCode': -9,
                                        'stopElapsedMs': 25.5, 'localizedDescription': 'private-content'})
            self.assertEqual(result['stopConfirmed'], state == 'completed')
            self.assertEqual(result['streamGeneration'], 'old-stream')
            self.assertEqual(result['platformErrorCode'], -9)
            self.assertNotIn('private-content', json.dumps(result))
        self.assertEqual(capture_lifecycle({'streamGeneration': 'private-content/URL'}), {})

    def test_ages_can_be_unknown_and_bool_is_not_a_counter(self):
        self.assertEqual(frame_progress({'lastBodyContentChangeAgeMs': None, 'bodySamples': True,
                                         'bodyContentChanges': 3, 'sourceFrame': {'pixelFormat': '420f'}}),
                         {'lastBodyContentChangeAgeMs': None, 'bodyContentChanges': 3,
                          'sourceFrame': {'pixelFormat': '420f'}})


if __name__ == '__main__':
    unittest.main()
