"""Isolated diagnostics tests: no real helper, mouse events or user configuration."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QUIETBRIDGE_CONFIG_DIR", tempfile.mkdtemp(prefix="quietbridge-test-config-"))
import server


class OperationLogTests(unittest.TestCase):
    def setUp(self):
        controls = mock.patch.object(server, "control_snapshot", return_value={"master": True})
        controls.start()
        self.addCleanup(controls.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.dict(os.environ, {"QUIETBRIDGE_DIAGNOSTICS_DIR": str(self.root), "QUIETBRIDGE_OPERATION_LOG": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def records(self):
        return [json.loads(line) for line in (self.root / "bridge-operations.jsonl").read_text().splitlines()]

    def test_only_geometry_identifiers_and_status_are_logged(self):
        sentinel = "synthetic-content-must-stay-out-of-logs"
        params = {"appHandle": "a", "windowId": 7, "x": 20, "y": 30,
                  "text": sentinel, "value": sentinel, "expectedText": sentinel, "key": sentinel}
        result = {"windowId": 7, "bounds": {"x": -900, "y": 100, "width": 400, "height": 200, "title": sentinel},
                  "pixelWidth": 800, "pixelHeight": 400, "data": sentinel, "tree": sentinel,
                  "pointer": {"windowId": 7, "targetPid": 88, "windowPoint": {"x": 20, "y": 30},
                              "screenPoint": {"x": -880, "y": 130}, "text": sentinel}}
        server.operation_log("fixture", target=server.diagnostic_fields(params), result=server.diagnostic_result(result))
        record = self.records()[0]
        self.assertNotIn(sentinel, json.dumps(record))
        self.assertEqual(record["result"]["pixelsPerPointX"], 2)
        self.assertEqual(record["result"]["pixelsPerPointY"], 2)
        self.assertEqual(record["result"]["pointer"]["screenPoint"], {"x": -880, "y": 130})
        self.assertEqual((self.root / "bridge-operations.jsonl").stat().st_mode & 0o777, 0o600)

    def test_render_probe_logging_keeps_fixed_stage_metadata_only(self):
        safe = dict(stage='capture', startedAt=100, completedAt=101, performed=True,
                    attachedBefore=True, attachedAfter=True, pageEnabledBefore=True,
                    pageEnabledAfter=True, captureDataLength=8)
        args = {'action': 'result', 'taskId': 'task', 'commandId': 'a' * 32}
        for result in (safe, dict(safe, data='private-content')):
            payload = {'operation': 'render_probe', 'status': 'ok', 'result': result}
            with mock.patch.object(server, '_events_handler', return_value=json.dumps(payload)):
                server._events_dispatch(args)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['result'], safe)
        self.assertNotIn('private-content', json.dumps(records))
        self.assertEqual(server.diagnostic_fields({'stage': 'attach', 'data': 'private-content'}), {'stage': 'attach'})
        self.assertEqual(server.diagnostic_fields({'stage': 'private-content'}), {})

    def test_invalid_fields_do_not_break_diagnostics(self):
        for value in (None, [], "invalid", 1):
            self.assertEqual(server.diagnostic_fields(value), {})
        self.assertEqual(server.diagnostic_fields({"x": float("nan"), "y": float("inf"), "windowId": True}), {})

    def test_scroll_request_logs_explicit_direction_and_legacy_delta_without_text(self):
        self.assertEqual(server.diagnostic_fields({'direction': 'down', 'lines': 6, 'value': 'private'}),
                         {'direction': 'down', 'lines': 6})
        self.assertEqual(server.diagnostic_fields({'direction': 'private', 'deltaY': -6}), {'deltaY': -6})

    def test_preview_host_and_capture_targets_are_logged_separately_without_ui_contents(self):
        preview = {"trackingWindowId": 7, "hostWindowId": 10, "hostPid": 42,
                   "hostSpaceIds": [8], "panelSpaceIds": [2], "panelSpaceVerified": False,
                   "mode": "application", "captureMode": "window", "visible": False,
                   "status": "synthetic-content-must-stay-out-of-logs",
                   "panelPlacementError": "synthetic-content-must-stay-out-of-logs"}
        result = server.diagnostic_result({"preview": preview})
        self.assertEqual(result['preview']['trackingWindowId'], 7)
        self.assertEqual(result['preview']['hostWindowId'], 10)
        self.assertEqual(result['preview']['hostSpaceIds'], [8])
        self.assertEqual(result['preview']['panelSpaceIds'], [2])
        self.assertFalse(result['preview']['panelSpaceVerified'])
        self.assertNotIn('synthetic-content', json.dumps(result))
        self.assertNotIn('hostSpaceIds', server.diagnostic_result({'preview': {'hostSpaceIds': ['private-content']}})['preview'])

    def test_preview_frame_progress_retains_only_finite_counters_without_content(self):
        preview = {'frameAgeMs': 12.5, 'videoFrames': 20, 'targetOnCurrentSpace': False,
                   'videoLayerReady': False, 'frameProgress': {'completeFrames': 20, 'submittedFrames': 2,
                   'backpressureDroppedFrames': 18, 'lastSubmissionAgeMs': 1500.5,
                   'lastSampledContentChangeAgeMs': None, 'contentSamples': float('nan'),
                   'sampleFailures': -1, 'callbackFrames': True, 'fingerprint': 'private-content',
                   'image': 'private-content'}}
        result = server.diagnostic_result({'preview': preview})['preview']
        self.assertEqual(result['frameProgress'], {'completeFrames': 20, 'submittedFrames': 2,
                         'backpressureDroppedFrames': 18, 'lastSubmissionAgeMs': 1500.5,
                         'lastSampledContentChangeAgeMs': None})
        self.assertEqual(result['frameAgeMs'], 12.5)
        self.assertFalse(result['targetOnCurrentSpace'])
        self.assertFalse(result['videoLayerReady'])
        self.assertNotIn('private-content', json.dumps(result, allow_nan=False))

    def test_source_probe_and_capture_stop_are_content_free_and_independent(self):
        preview = {'frameProgress': {'bodySamples': 5, 'bodyContentChanges': 1,
                   'bodyNearUniformSamples': 4, 'lastBodyContentChangeAgeMs': None,
                   'sourceFrame': {'pixelFormat': '420v', 'width': 800, 'bodyChannelSpread': 0,
                   'bodyChromaSpread': 1, 'bodyNearUniform': True, 'colorPrimaries': 'ITU_R_709_2',
                   'transferFunction': 'private-content', 'fingerprint': 'private-content'}},
                   'captureLifecycle': {'streamGeneration': 'new', 'trackingWindowId': 8,
                                        'startState': 'completed', 'stopState': 'not_requested'},
                   'captureStop': {'streamGeneration': 'old', 'trackingWindowId': 7,
                                   'stopState': 'requested', 'stopConfirmed': True,
                                   'platformErrorDomain': 'private-content', 'message': 'private-content'}}
        result = server.diagnostic_result({'preview': preview})['preview']
        self.assertEqual(result['frameProgress']['bodyContentChanges'], 1)
        self.assertTrue(result['frameProgress']['sourceFrame']['bodyNearUniform'])
        self.assertEqual(result['captureLifecycle']['streamGeneration'], 'new')
        self.assertEqual(result['captureStop']['streamGeneration'], 'old')
        self.assertFalse(result['captureStop']['stopConfirmed'])
        self.assertNotIn('private-content', json.dumps(result, allow_nan=False))

    def test_disabled_logging_does_not_create_files(self):
        with mock.patch.dict(os.environ, {"QUIETBRIDGE_OPERATION_LOG": "0"}):
            server.operation_log("fixture")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_file_errors_do_not_replace_operation_results(self):
        path = self.root / "ordinary-file"
        path.write_text("fixture")
        with mock.patch.dict(os.environ, {"QUIETBRIDGE_DIAGNOSTICS_DIR": str(path)}):
            server.operation_log("fixture")
        self.assertEqual(path.read_text(), "fixture")

    def test_rotation_preserves_complete_json_lines_with_bounded_files(self):
        with mock.patch.object(server, "OPERATION_LOG_MAX_BYTES", 300):
            for index in range(10):
                server.operation_log("fixture", index=index)
        path = self.root / "bridge-operations.jsonl"
        previous = self.root / "bridge-operations.jsonl.1"
        for log in (previous, path):
            self.assertLessEqual(log.stat().st_size, 300)
            for line in log.read_text().splitlines():
                json.loads(line)
        self.assertEqual(self.records()[-1]["index"], 9)

    def test_native_request_id_correlates_dispatch_and_response(self):
        client = server.HelperClient("/not-launched")
        process = mock.Mock()
        process.poll.return_value = None
        client.proc = process
        client.connection_id = "fixture"

        def respond(data):
            request = json.loads(data)
            client.responses[request["id"]] = {"id": request["id"], "result": {"dispatched": True, "verified": False}}
            return len(data)

        process.stdin.write.side_effect = respond
        with mock.patch.object(server, "TASK_DISPATCH", None), mock.patch.object(server, "INPUT_CONNECTION", None):
            result = client._request_once("click", {"appHandle": "a", "windowId": 7, "x": 20, "y": 30}, 1)
        self.assertTrue(result["dispatched"])
        records = self.records()
        self.assertEqual([row["event"] for row in records], ["helper.request", "helper.sent", "helper.completed"])
        self.assertEqual(len({row["nativeRequestId"] for row in records}), 1)
        self.assertEqual(records[-1]["result"]["verified"], False)

    def test_validation_failure_logs_tool_trace_and_never_starts_helper(self):
        state = {"app": "a", "apps": {"a": {"pid": 88, "name": "fixture"}}, "window": {}}
        with mock.patch.object(server, "TASK_FILE", str(self.root / "absent-task.json")), \
                mock.patch.object(server, "TASK_BINDINGS", {}), mock.patch.object(server, "STATE", state), \
                mock.patch.object(server, "READ_ONLY", False), mock.patch.object(server, "ALLOWLIST", []), \
                mock.patch.object(server.HELPER, "request", side_effect=AssertionError("must not dispatch")):
            result = server.call_tool("quietbridge_click", {"appHandle": "a", "windowId": 7})
        self.assertTrue(result["isError"])
        records = self.records()
        trace_id = result["structuredContent"]["diagnostics"]["toolTraceId"]
        self.assertEqual([row["toolTraceId"] for row in records], [trace_id, trace_id])
        self.assertEqual(records[-1]["execution"], "not_dispatched")
        self.assertEqual(records[-1]["errorCode"], "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
