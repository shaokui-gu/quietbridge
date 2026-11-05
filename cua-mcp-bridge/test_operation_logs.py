"""Isolated diagnostics tests: no real helper, mouse events or user configuration."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("CUA_CONFIG_DIR", tempfile.mkdtemp(prefix="cua-test-config-"))
import server


class OperationLogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.dict(os.environ, {"CUA_DIAGNOSTICS_DIR": str(self.root), "CUA_OPERATION_LOG": "1"})
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

    def test_invalid_fields_do_not_break_diagnostics(self):
        for value in (None, [], "invalid", 1):
            self.assertEqual(server.diagnostic_fields(value), {})
        self.assertEqual(server.diagnostic_fields({"x": float("nan"), "y": float("inf"), "windowId": True}), {})

    def test_disabled_logging_does_not_create_files(self):
        with mock.patch.dict(os.environ, {"CUA_OPERATION_LOG": "0"}):
            server.operation_log("fixture")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_file_errors_do_not_replace_operation_results(self):
        path = self.root / "ordinary-file"
        path.write_text("fixture")
        with mock.patch.dict(os.environ, {"CUA_DIAGNOSTICS_DIR": str(path)}):
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
            result = server.call_tool("cua_click", {"appHandle": "a", "windowId": 7})
        self.assertTrue(result["isError"])
        records = self.records()
        trace_id = result["structuredContent"]["diagnostics"]["toolTraceId"]
        self.assertEqual([row["toolTraceId"] for row in records], [trace_id, trace_id])
        self.assertEqual(records[-1]["execution"], "not_dispatched")
        self.assertEqual(records[-1]["errorCode"], "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
