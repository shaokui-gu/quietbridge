import json
from pathlib import Path
import unittest
from unittest import mock

# 运行态默认目录是真实用户目录；测试必须隔离。
import os
import tempfile
os.environ.setdefault('QUIETBRIDGE_CONFIG_DIR', tempfile.mkdtemp(prefix='quietbridge-test-config-'))

import server


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        core = mock.patch.object(server, "ROUTING_ENABLED", False)
        core.start()
        self.addCleanup(core.stop)
        for patcher in (mock.patch.object(server, "TASK_FILE", "/nonexistent/browser-test-task.json"),
                        mock.patch.object(server, "control_snapshot", return_value={"master": True}),
                        mock.patch.object(server, "TASK_BINDINGS", {})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def rpc(self, arguments):
        return server.handle({
            "jsonrpc": "2.0", "id": "plan-test", "method": "tools/call",
            "params": {"name": "quietbridge_browser_session", "arguments": arguments},
        })["result"]

    def test_fake_helper_forwards_prepare_and_list_once_in_read_only_mode(self):
        for action in ("prepare", "list"):
            payload = {"state": "prepared", "sessionId": "fixture-plan", "pid": None} if action == "prepare" else {"sessions": []}
            fake = mock.Mock(spec=server.HelperClient)
            fake.request.return_value = payload
            with self.subTest(action=action), mock.patch.object(server, "HELPER", fake), \
                    mock.patch.object(server, "READ_ONLY", True), \
                    mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("测试不能启动 helper 或浏览器")), \
                    mock.patch.object(server.os, "makedirs", side_effect=AssertionError("测试不能创建目录")):
                result = self.rpc({"action": action})
                self.assertFalse(result.get("isError", False))
                self.assertEqual(json.loads(result["content"][0]["text"]), payload)
                fake.request.assert_called_once_with("browserSession", {"action": action}, timeout=15, retry_safe=False)

    def test_rejects_invalid_actions_extra_paths_and_arguments_before_forwarding(self):
        cases = [None, {}, [], ["prepare"], "prepare", True, 1,
                 {"action": None}, {"action": True}, {"action": 1}, {"action": ["prepare"]},
                 {"action": "create"}, {"action": "close"}, {"action": "PREPARE"}, {"action": ""}]
        for key in ("profile", "applicationPath", "arguments", "sessionId", "appHandle", "activates", "createsNewApplicationInstance", "url", "space"):
            cases.append({"action": "prepare", key: "/tmp/not-allowed"})
        fake = mock.Mock(spec=server.HelperClient)
        with mock.patch.object(server, "HELPER", fake):
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    result = self.rpc(arguments)
                    self.assertTrue(result["isError"])
                    self.assertIn("INVALID_ARGUMENT", result["content"][0]["text"])
        fake.request.assert_not_called()

    def test_helper_errors_are_not_hidden_or_replayed(self):
        for code in ("SESSION_LOCKED", "SESSION_UNAVAILABLE", "PERMISSION_DENIED", "HELPER_TIMEOUT"):
            fake = mock.Mock(spec=server.HelperClient)
            fake.request.side_effect = server.HelperError(code, "测试错误")
            with self.subTest(code=code), mock.patch.object(server, "HELPER", fake):
                result = self.rpc({"action": "prepare"})
                self.assertTrue(result["isError"])
                self.assertIn(code, result["content"][0]["text"])
                self.assertEqual(fake.request.call_count, 1)

    def test_broken_pipe_does_not_retry_either_action(self):
        client = server.HelperClient("/not-executed")
        for action in ("prepare", "list"):
            with self.subTest(action=action), mock.patch.object(server, "HELPER", client), \
                    mock.patch.object(client, "_request_once", side_effect=BrokenPipeError("测试管道断开")) as once, \
                    mock.patch.object(client, "_start", side_effect=AssertionError("不得启动进程")), \
                    mock.patch.object(client, "_stop") as stop:
                result = self.rpc({"action": action})
                self.assertTrue(result["isError"])
                self.assertIn("HELPER_BROKEN", result["content"][0]["text"])
                once.assert_called_once_with("browserSession", {"action": action}, 15)
                stop.assert_not_called()

    def test_start_forwards_strict_confirmation_once_and_read_only_blocks(self):
        args = {"action": "start", "sessionId": "12345678-1234-1234-1234-123456789ABC", "confirm": True}
        fake = mock.Mock(spec=server.HelperClient)
        fake.request.return_value = {"state": "pending", "dispatched": True}
        with mock.patch.object(server, "HELPER", fake), mock.patch.object(server, "READ_ONLY", False), \
                mock.patch.object(server, "ALLOWLIST", []):
            self.assertFalse(self.rpc(args).get("isError", False))
            fake.request.assert_called_once_with("browserSession", args, timeout=15, retry_safe=False)
        fake.reset_mock()
        with mock.patch.object(server, "HELPER", fake), mock.patch.object(server, "READ_ONLY", True):
            result = self.rpc(args)
            self.assertTrue(result["isError"])
            self.assertIn("READ_ONLY", result["content"][0]["text"])
            fake.request.assert_not_called()

    def test_start_rejects_missing_confirmation_and_configuration_injection(self):
        valid = {"action": "start", "sessionId": "12345678-1234-1234-1234-123456789ABC", "confirm": True}
        cases = [{"action": "start"}, dict(valid, confirm=False), dict(valid, confirm=1),
                 dict(valid, sessionId="invalid"), dict(valid, sessionId=1), dict(valid, profile="/tmp/a"),
                 dict(valid, arguments=["--unsafe"]), dict(valid, applicationPath="/tmp/app")]
        fake = mock.Mock(spec=server.HelperClient)
        with mock.patch.object(server, "HELPER", fake):
            for args in cases:
                with self.subTest(args=args):
                    self.assertIn("INVALID_ARGUMENT", self.rpc(args)["content"][0]["text"])
        fake.request.assert_not_called()

    def test_start_pipe_failure_is_never_retried(self):
        args = {"action": "start", "sessionId": "12345678-1234-1234-1234-123456789ABC", "confirm": True}
        client = server.HelperClient("/not-executed")
        with mock.patch.object(server, "HELPER", client), mock.patch.object(server, "READ_ONLY", False), \
                mock.patch.object(server, "ALLOWLIST", []), \
                mock.patch.object(client, "_request_once", side_effect=BrokenPipeError("模拟断开")) as once:
            self.assertIn("HELPER_BROKEN", self.rpc(args)["content"][0]["text"])
            once.assert_called_once_with("browserSession", args, 15)

    def test_start_transport_timeout_keeps_connection_and_late_response(self):
        client = server.HelperClient("/not-executed")
        client.proc = mock.Mock()
        args = {"action": "start", "sessionId": "12345678-1234-1234-1234-123456789ABC", "confirm": True}
        with mock.patch.object(client, "_start"), mock.patch.object(client, "_stop") as stop, \
                mock.patch.object(server.time, "monotonic", side_effect=[100, 100, 102, 103, 104, 104, 106]):
            with self.assertRaises(server.HelperError) as raised:
                client._request_once("browserSession", args, 1)
            self.assertEqual(raised.exception.code, "BROWSER_LAUNCH_UNCERTAIN")
            rid = client.pending_browser_response
            self.assertIsNotNone(rid)
            stop.assert_not_called()
            with self.assertRaises(server.HelperError):
                client._request_once("browserSession", {"action": "list"}, 1)
            self.assertEqual(client.proc.stdin.write.call_count, 1)
            client.responses[rid] = {"result": {"state": "pending"}}
            # 下一请求消费迟到包后仍使用原连接；模拟 list 自身的超时仅用于结束等待。
            with self.assertRaises(server.HelperError):
                client._request_once("browserSession", {"action": "list"}, 1)
            self.assertIsNone(client.pending_browser_response)
            self.assertEqual(client.proc.stdin.write.call_count, 2)

    def test_attach_forwards_once_and_read_only_blocks_without_dispatch(self):
        args = {"action": "attach", "pid": 88, "windowId": 9, "confirm": True}
        fake = mock.Mock(spec=server.HelperClient)
        fake.request.return_value = {"state": "attached", "windowId": 9}
        with mock.patch.object(server, "HELPER", fake), mock.patch.object(server, "READ_ONLY", False), \
                mock.patch.object(server, "ALLOWLIST", []):
            self.assertFalse(self.rpc(args).get("isError", False))
            fake.request.assert_called_once_with("browserSession", args, timeout=15, retry_safe=False)
        fake.reset_mock()
        with mock.patch.object(server, "HELPER", fake), mock.patch.object(server, "READ_ONLY", True):
            self.assertIn("READ_ONLY", self.rpc(args)["content"][0]["text"])
            fake.request.assert_not_called()

    def test_attach_rejects_missing_confirmation_invalid_ids_and_extra_fields(self):
        valid = {"action": "attach", "pid": 88, "windowId": 9, "confirm": True}
        cases = [{k: v for k, v in valid.items() if k != key} for key in ("pid", "windowId", "confirm")]
        cases += [dict(valid, confirm=False), dict(valid, confirm=1), dict(valid, sessionId="unexpected"),
                  dict(valid, profile="/tmp/a")]
        for key in ("pid", "windowId"):
            for value in (None, True, "9", 0, -1, 1.5, 4294967296):
                cases.append(dict(valid, **{key: value}))
        cases.append(dict(valid, pid=2147483648))
        for action in ("prepare", "list", "start"):
            cases.append(dict(valid, action=action))
        fake = mock.Mock(spec=server.HelperClient)
        with mock.patch.object(server, "HELPER", fake):
            for args in cases:
                with self.subTest(args=args):
                    self.assertIn("INVALID_ARGUMENT", self.rpc(args)["content"][0]["text"])
        fake.request.assert_not_called()

    def test_attach_pipe_failure_and_helper_errors_never_retry(self):
        args = {"action": "attach", "pid": 88, "windowId": 9, "confirm": True}
        client = server.HelperClient("/not-executed")
        for error in (BrokenPipeError("模拟断开"), server.HelperError("HELPER_TIMEOUT", "模拟超时")):
            with self.subTest(error=error), mock.patch.object(server, "HELPER", client), \
                    mock.patch.object(server, "READ_ONLY", False), mock.patch.object(server, "ALLOWLIST", []), \
                    mock.patch.object(client, "_request_once", side_effect=error) as once, \
                    mock.patch.object(client, "_start", side_effect=AssertionError("不得启动进程")):
                self.assertTrue(self.rpc(args)["isError"])
                once.assert_called_once_with("browserSession", args, 15)

    def test_attach_allowlist_rejection_does_not_dispatch(self):
        fake = mock.Mock(spec=server.HelperClient)
        with mock.patch.object(server, "HELPER", fake), mock.patch.object(server, "READ_ONLY", False), \
                mock.patch.object(server, "ALLOWLIST", ["com.other.app"]):
            self.assertIn("APP_NOT_ALLOWED", self.rpc({"action": "attach", "pid": 88, "windowId": 9, "confirm": True})["content"][0]["text"])
        fake.request.assert_not_called()

    def test_tool_and_native_schema_agree_on_read_only_surface(self):
        with mock.patch.object(server, "READ_ONLY", True):
            tools = server.handle({"id": "list", "method": "tools/list"})["result"]["tools"]
        tool = next(tool for tool in tools if tool["name"] == "quietbridge_browser_session")
        schema = tool["inputSchema"]
        self.assertEqual(schema["required"], ["action"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["action"], {"type": "string", "enum": ["prepare", "list", "start", "attach"]})
        self.assertEqual(schema["properties"]["confirm"], {"type": "boolean", "const": True})
        self.assertTrue(server.TOOL_MAP["quietbridge_browser_session"]["readOnly"])
        native_path = Path(__file__).resolve().parents[2] / "apps/macos/protocol.schema.json"
        native = json.loads(native_path.read_text())
        branch = next(branch for branch in native["oneOf"] if branch["properties"]["method"].get("const") == "browserSession")
        self.assertEqual(branch["properties"]["params"], schema)
        self.assertNotIn("browserSession", server.READ_ONLY_METHODS)


if __name__ == "__main__":
    unittest.main()
