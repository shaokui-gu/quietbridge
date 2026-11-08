import json
import unittest
from unittest import mock
# 运行态默认目录是真实用户目录；测试必须隔离。
import os
import tempfile
os.environ.setdefault('QUIETBRIDGE_CONFIG_DIR', tempfile.mkdtemp(prefix='quietbridge-test-config-'))

import server


class VisualStateTests(unittest.TestCase):
    def setUp(self):
        core = mock.patch.object(server, "ROUTING_ENABLED", False)
        core.start()
        self.addCleanup(core.stop)
        for patcher in (mock.patch.object(server, "TASK_FILE", "/nonexistent/visual-test-task.json"),
                        mock.patch.object(server, "control_snapshot", return_value={"master": True}),
                        mock.patch.object(server, "TASK_BINDINGS", {}),
                        mock.patch.object(server, "ensure_share", return_value=None),
                        mock.patch.object(server, "READ_ONLY", False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = {"app": "a", "apps": {"a": {"name": "fixture"}}, "window": {},
                      "snapshot": None, "snapshot_app": None, "snapshot_window": None}
        self.payload = {"data": "iVBORw0KGgo=", "mimeType": "image/png", "windowId": 7,
                        "snapshotId": "visual:1", "frameId": "frame:1", "bounds": {"x": -10, "y": 20, "width": 320, "height": 200},
                        "pixelWidth": 640, "pixelHeight": 400, "capturedAtMonotonicSeconds": 10, "ageMs": 1}

    def rpc(self, args):
        return server.handle({"id": "v", "method": "tools/call", "params": {"name": "quietbridge_visual_state", "arguments": args}})["result"]

    def test_fake_helper_image_and_coordinates_forwarded_read_only(self):
        fake = mock.Mock(spec=server.HelperClient)
        fake.connection_id = "visual-test-connection"
        fake.request.return_value = self.payload
        with mock.patch.object(server, "HELPER", fake), mock.patch.object(server, "STATE", self.state), \
                mock.patch.object(server, "READ_ONLY", False), mock.patch.object(server.os.path, "isdir", return_value=False), \
                mock.patch.object(server.os, "makedirs", side_effect=AssertionError("不得创建目录")):
            result = self.rpc({"appHandle": "a", "windowId": 7})
        self.assertEqual(result["content"][0], {"type": "image", "data": self.payload["data"], "mimeType": "image/png"})
        metadata = json.loads(result["content"][1]["text"].split("\n")[0])
        self.assertEqual(metadata["snapshotId"], "visual:1")
        self.assertEqual(metadata["bounds"], self.payload["bounds"])
        self.assertNotIn("data", metadata)
        self.assertEqual(self.state["snapshot_window"], 7)
        fake.request.assert_called_once_with("visualState", {"appHandle": "a", "windowId": 7}, timeout=8, retry_safe=True)

    def test_schema_and_read_only_listing(self):
        with mock.patch.object(server, "READ_ONLY", True):
            tools = server.handle({"id": "list", "method": "tools/list"})["result"]["tools"]
        tool = next(t for t in tools if t["name"] == "quietbridge_visual_state")
        self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertEqual(set(tool["inputSchema"]["properties"]), {"appHandle", "windowId"})
        self.assertIn("AXWebArea", tool["description"])

    def test_invalid_fields_never_reach_helper(self):
        fake = mock.Mock(spec=server.HelperClient)
        fake.connection_id = "visual-test-connection"
        with mock.patch.object(server, "HELPER", fake), mock.patch.object(server, "STATE", self.state):
            for args in ({"windowId": True}, {"windowId": 0}, {"windowId": "7"}, {"click": True}, {"appHandle": 3}):
                with self.subTest(args=args):
                    self.assertTrue(self.rpc(args)["isError"])
        fake.request.assert_not_called()

    def test_permission_owner_and_timeout_errors_are_not_fallback_success(self):
        for code in ("PERMISSION_DENIED", "SESSION_LOCKED", "STALE_APP", "WINDOW_CHANGED", "NATIVE_TIMEOUT"):
            fake = mock.Mock(spec=server.HelperClient)
            fake.connection_id = "visual-test-connection"
            fake.request.side_effect = server.HelperError(code, "fixture")
            with self.subTest(code=code), mock.patch.object(server, "HELPER", fake), mock.patch.object(server, "STATE", self.state):
                result = self.rpc({"windowId": 7})
                self.assertTrue(result["isError"])
                self.assertIn(code, result["content"][0]["text"])
                self.assertEqual(fake.request.call_count, 1)

    def test_existing_output_directory_only(self):
        fake = mock.Mock(spec=server.HelperClient)
        fake.connection_id = "visual-test-connection"
        fake.request.return_value = self.payload
        output = mock.MagicMock()
        output.__enter__.return_value.name = "/authorized/shots/visual-fixture.png"
        with mock.patch.object(server, "HELPER", fake), mock.patch.object(server, "STATE", self.state), \
                mock.patch.object(server.os.path, "isdir", return_value=True), \
                mock.patch("tempfile.NamedTemporaryFile", return_value=output) as opened:
            result = self.rpc({"windowId": 7})
        self.assertFalse(result.get("isError", False))
        self.assertEqual(opened.call_args.kwargs["dir"], server.SHOTS_DIR)
        output.__enter__.return_value.write.assert_called_once()


class WaitReadyTests(unittest.TestCase):
    def setUp(self):
        self.state = {"app": "a", "apps": {"a": {"pid": 88}}, "window": {},
                      "snapshot": None, "snapshot_app": None, "snapshot_window": None}
        self.args = {"appHandle": "a", "windowId": 7, "expectedUrl": "https://example.com/new", "expectedText": "目标正文"}
        self.ax = {"windowId": 7, "appHandle": "a", "pid": 88, "snapshotId": "ax:1", "tree": ""}
        self.visual = {"windowId": 7, "appHandle": "a", "pid": 88, "snapshotId": "v:1", "data": "fixture", "mimeType": "image/png"}
        self.now = 100.0
        self.waits = []
        self.fake = mock.Mock(spec=server.HelperClient)
        self.fake.connection_id = "connection-1"
        self.fake._alive.return_value = True
        self.fake.request.side_effect = lambda method, *a, **k: dict(self.ax if method == "getState" else self.visual)
        for patcher in (mock.patch.object(server, "STATE", self.state),
                        mock.patch.object(server, "control_snapshot", return_value={"master": True}),
                        mock.patch.object(server, "HELPER", self.fake),
                        mock.patch.object(server, "TASK_FILE", "/nonexistent/wait-test-task.json"),
                        mock.patch.object(server, "TASK_BINDINGS", {}),
                        mock.patch.object(server, "TASK_DISPATCH", None),
                        mock.patch.object(server, "WAIT_DEADLINE", None),
                        mock.patch.object(server, "READ_ONLY", False),
                        mock.patch.object(server, "ensure_share", return_value=None),
                        mock.patch.object(server.time, "monotonic", side_effect=lambda: self.now),
                        mock.patch.object(server.threading.Event, "wait", side_effect=self.wait),
                        mock.patch.object(server.time, "sleep", side_effect=AssertionError("不得sleep")),
                        mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("不得启动进程"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def tree(self, url=None, text=None, prefix=""):
        return prefix + '- [0] AXWindow\n  - [1] AXWebArea "页面标题" url=' + json.dumps(url or self.args["expectedUrl"]) + '\n    - [2] AXStaticText ' + json.dumps(text or self.args["expectedText"], ensure_ascii=False)

    def rpc(self, **overrides):
        return server.call_tool("quietbridge_wait_ready", dict(self.args, **overrides))

    def metadata(self, result):
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][-1]["text"].split("\n")[0])

    def test_two_consecutive_matches_and_schema(self):
        self.ax["tree"] = self.tree()
        result = self.rpc()
        meta = self.metadata(result)
        self.assertTrue(meta["ready"])
        self.assertEqual(meta["completionEvidence"], "ax_url_and_content")
        self.assertEqual(meta["consecutiveMatches"], 2)
        self.assertEqual(self.waits, [2])
        self.assertEqual([c.args[0] for c in self.fake.request.call_args_list], ["getState", "getState"])
        self.assertEqual([c.kwargs["timeout"] for c in self.fake.request.call_args_list], [30, 28])
        self.assertTrue(all(c.kwargs["retry_safe"] is False for c in self.fake.request.call_args_list))
        self.assertEqual(self.state["snapshot_window"], 7)
        tool = server.TOOL_MAP["quietbridge_wait_ready"]
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertEqual(tool["inputSchema"]["required"], ["appHandle", "windowId", "expectedUrl", "expectedText"])
        self.assertFalse(tool["inputSchema"]["additionalProperties"])

    def test_old_page_wrong_url_and_domain_prefix_never_ready(self):
        for url in ("https://example.com/old", "http://example.com/new", "https://example.com.evil/new",
                    "https://example.com/newer", "https://example.com:444/new", "https://evil.example.com/new",
                    "https://example.com/new?other=1", "https://example.com@evil.test/new"):
            with self.subTest(url=url):
                self.ax["tree"] = self.tree(url)
                result = self.rpc(timeoutSeconds=3, intervalSeconds=1)
                self.assertFalse(self.metadata(result)["ready"])
                self.assertEqual(self.metadata(result)["completionEvidence"], "unknown")

    def test_url_normalization_fragment_and_query(self):
        normalized = server.normalized_page_url
        for url in ("HTTPS://EXAMPLE.COM:443/new#section", "https://example.com/a/../new", "https://example.com/%6Eew"):
            self.assertEqual(normalized(url), normalized(self.args["expectedUrl"]))
            self.ax["tree"] = self.tree(url)
            self.assertTrue(self.metadata(self.rpc())["ready"])
        self.assertEqual(normalized("https://example.com"), normalized("https://example.com/"))
        self.assertEqual(normalized("https://[0:0:0:0:0:0:0:1]:443/"), normalized("https://[::1]/"))
        self.assertNotEqual(normalized("https://example.com/new/"), normalized(self.args["expectedUrl"]))

    def test_webarea_mixing_nested_frames_and_titles_rejected(self):
        url = 'url="https://example.com/new"'
        trees = ('', '- [0] AXWindow "目标正文"',
                 '- [0] AXWebArea "目标正文" ' + url,
                 '- [0] AXWebArea ' + url + '\n- [1] AXWebArea url="https://other.test/"\n  - AXStaticText "目标正文"',
                 '- [0] AXWebArea ' + url + '\n  - AXWebArea url="https://other.test/"\n    - AXStaticText "目标正文"',
                 '- AXToolbar\n  - AXStaticText "目标正文"\n- AXWebArea ' + url,
                 '- AXWebArea ' + url + '\n  - AXToolbar\n    - AXStaticText "目标正文"',
                 '- AXWebArea ' + url + '\n  - AXGroup "目标正文"',
                 '- AXTextField "AXWebArea 目标正文"',
                 '- AXWebAreaFake ' + url + '\n  - AXStaticText "目标正文"',
                 '- AXWebArea ' + url + '\n  - AXStaticText help="目标正文"',
                 '- AXWebArea description="url=\\"https://example.com/new\\""\n  - AXStaticText "目标正文"')
        for tree in trees:
            with self.subTest(tree=tree):
                self.assertFalse(server.ax_webarea_matches(tree, self.args["expectedUrl"], "目标正文"))

    def test_formatter_indentation_transparent_containers_and_escaped_text(self):
        for tree in ('- AXWebArea url="https://example.com/new"\n  - AXStaticText "目标正文"',
                     '- AXWindow\n  - AXGroup "容器"\n    - AXWebArea url="https://example.com/new"\n      - AXStaticText selectionWritable=unknown "目标正文"',
                     '- AXWebArea url="https://example.com/new"\n  - AXStaticText = "目标正文"'):
            self.assertTrue(server.ax_webarea_matches(tree, self.args["expectedUrl"], "目标正文"))
        tree = '- AXWebArea url="https://example.com/new"\n  - AXStaticText "line\\n目标正文"'
        self.assertTrue(server.ax_webarea_matches(tree, self.args["expectedUrl"], 'line\n目标正文'))
        for indent in (" ", "   ", "    ", "\t"):
            self.assertFalse(server.ax_webarea_matches('- AXWebArea url="https://example.com/new"\n' + indent + '- AXStaticText "目标正文"', self.args["expectedUrl"], "目标正文"))

    def test_state_loss_resets_consecutive_matches(self):
        sequence = iter([self.tree(), self.tree(text="未完成"), self.tree(), self.tree()])
        def read(method, *a, **k):
            return dict(self.ax, tree=next(sequence)) if method == "getState" else dict(self.visual)
        self.fake.request.side_effect = read
        self.assertTrue(self.metadata(self.rpc())["ready"])
        self.assertEqual(sum(c.args[0] == "getState" for c in self.fake.request.call_args_list), 4)
        self.assertEqual(self.waits, [2, 2, 2])

    def test_different_webarea_identity_does_not_count_as_consecutive(self):
        first = '- AXWebArea url="https://example.com/new"\n  - AXStaticText "目标正文"'
        second = '- AXGroup "other"\n' + first
        sequence = iter([first, second, second])
        self.fake.request.side_effect = lambda *a, **k: dict(self.ax, tree=next(sequence))
        self.assertTrue(self.metadata(self.rpc())["ready"])
        self.assertEqual(self.fake.request.call_count, 3)

    def test_ax_missing_polls_until_budget_and_retains_latest_of_three_frames(self):
        count = 0
        def read(method, *a, **k):
            nonlocal count
            if method == "getState":
                return dict(self.ax)
            count += 1
            return dict(self.visual, data="frame-%s" % count)
        self.fake.request.side_effect = read
        result = self.rpc(timeoutSeconds=6, intervalSeconds=1)
        meta = self.metadata(result)
        self.assertEqual(meta["status"], "needs_visual_review")
        self.assertEqual(meta["remainingSeconds"], 0)
        self.assertEqual(meta["visualFrames"], 3)
        self.assertEqual(result["content"][0]["data"], "frame-3")
        self.assertEqual(len(result["content"]), 2)
        self.assertEqual(sum(c.args[0] == "getState" for c in self.fake.request.call_args_list), 6)
        self.assertEqual(self.waits, [1] * 6)

    def test_single_match_is_timeout_not_ready(self):
        self.ax["tree"] = self.tree()
        meta = self.metadata(self.rpc(timeoutSeconds=1, intervalSeconds=2))
        self.assertEqual(meta["status"], "timeout")
        self.assertEqual(meta["completionEvidence"], "unknown")
        self.assertEqual(self.fake.request.call_count, 1)
        self.assertEqual(self.waits, [1])

    def test_visual_uses_remaining_budget_and_interval_does_not_reset_deadline(self):
        def read(method, *a, **k):
            self.now += 4 if method == "getState" else 0.5
            return dict(self.ax if method == "getState" else self.visual)
        self.fake.request.side_effect = read
        meta = self.metadata(self.rpc(timeoutSeconds=5, intervalSeconds=1))
        self.assertEqual(meta["status"], "needs_visual_review")
        self.assertEqual([c.kwargs["timeout"] for c in self.fake.request.call_args_list], [5, 1])
        self.assertEqual(self.waits, [0.5])

    def test_late_ax_match_rejected_without_new_request(self):
        def read(*a, **k):
            self.now += 30
            return dict(self.ax, tree=self.tree())
        self.fake.request.side_effect = read
        self.assertEqual(self.metadata(self.rpc())["status"], "timeout")
        self.assertEqual(self.fake.request.call_count, 1)

    def test_late_visual_is_retained_but_never_ready(self):
        def read(method, *a, **k):
            self.now += 1 if method == "getState" else 29
            return dict(self.ax if method == "getState" else self.visual)
        self.fake.request.side_effect = read
        result = self.rpc()
        self.assertEqual(self.metadata(result)["status"], "needs_visual_review")
        self.assertEqual(result["content"][0]["type"], "image")
        self.assertEqual(self.waits, [])

    def test_native_errors_immediately_propagate_without_retry(self):
        for code in ("PERMISSION_DENIED", "SESSION_LOCKED", "STALE_APP", "WINDOW_CHANGED", "WINDOW_UNAVAILABLE", "HELPER_TIMEOUT", "NATIVE_TIMEOUT"):
            for stage in ("getState", "visualState"):
                def read(method, *a, **k):
                    if method == stage:
                        raise server.HelperError(code, "原始错误")
                    return dict(self.ax)
                self.fake.reset_mock()
                self.fake.request.side_effect = read
                with self.subTest(code=code, stage=stage):
                    result = self.rpc()
                    self.assertTrue(result["isError"])
                    self.assertIn(code + ": 原始错误", result["content"][0]["text"])
                    self.assertEqual(self.fake.request.call_count, 1 if stage == "getState" else 2)
                    self.assertTrue(all(c.kwargs["retry_safe"] is False for c in self.fake.request.call_args_list))

    def test_changed_response_identity_and_incomplete_frames_stop(self):
        for stage in ("getState", "visualState"):
            cases = [({"windowId": 8}, "WINDOW_CHANGED"), ({"appHandle": "other"}, "STALE_APP"), ({"pid": 99}, "STALE_APP")]
            if stage == "visualState":
                cases += [({"data": ""}, "NATIVE_ERROR"), ({"mimeType": "image/jpeg"}, "NATIVE_ERROR")]
            for overrides, code in cases:
                def read(method, *a, **k):
                    result = dict(self.ax if method == "getState" else self.visual)
                    if method == stage:
                        result.update(overrides)
                    return result
                self.fake.request.side_effect = read
                with self.subTest(stage=stage, overrides=overrides):
                    result = self.rpc()
                    self.assertTrue(result["isError"])
                    self.assertIn(code, result["content"][0]["text"])

    def test_missing_ax_between_matches_resets_streak(self):
        sequence = iter([self.tree(), "", self.tree(), self.tree()])
        self.fake.request.side_effect = lambda method, *a, **k: dict(self.ax, tree=next(sequence)) if method == "getState" else dict(self.visual)
        self.assertTrue(self.metadata(self.rpc())["ready"])
        self.assertEqual(sum(c.args[0] == "getState" for c in self.fake.request.call_args_list), 4)

    def test_second_poll_process_change_immediately_stops(self):
        self.fake.request.side_effect = [dict(self.ax, tree=self.tree()), dict(self.ax, pid=99, tree=self.tree())]
        self.assertIn("STALE_APP", self.rpc()["content"][0]["text"])
        self.assertEqual(self.fake.request.call_count, 2)

    def test_read_only_helper_deadline_prevents_send_after_startup(self):
        client = server.HelperClient("/not-executed")
        client.proc = mock.Mock()
        with mock.patch.object(client, "_start"), mock.patch.object(server.time, "monotonic", side_effect=[100, 102]):
            with self.assertRaises(server.HelperError) as raised:
                client._request_once("getState", {}, 1)
        self.assertEqual(raised.exception.code, "HELPER_TIMEOUT")
        client.proc.stdin.write.assert_not_called()

    def test_connection_change_stops_without_retry(self):
        def read(*a, **k):
            self.fake.connection_id = "replacement"
            return dict(self.ax)
        self.fake.request.side_effect = read
        self.assertIn("REQUIRES_REVALIDATION", self.rpc()["content"][0]["text"])
        self.assertEqual(self.fake.request.call_count, 1)

    def test_missing_pid_never_produces_ready(self):
        self.ax.pop("pid")
        self.ax["tree"] = self.tree()
        self.assertFalse(self.metadata(self.rpc(timeoutSeconds=3))["ready"])

    def test_invalid_arguments_do_not_reach_helper(self):
        cases = [{"appHandle": " "}, {"windowId": True}, {"windowId": "7"}, {"windowId": 0},
                 {"expectedText": ""}, {"expectedText": " "}, {"expectedText": "x" * 201}, {"expectedText": None},
                 {"timeoutSeconds": 0}, {"timeoutSeconds": 31}, {"timeoutSeconds": True}, {"timeoutSeconds": float("nan")},
                 {"intervalSeconds": 0.9}, {"intervalSeconds": float("inf")}, {"unexpected": 1}]
        cases += [{"expectedUrl": url} for url in (None, "", "file:///tmp/a", "example.com", "https:///new", "https://u:p@example.com/", "https://@example.com/", "https://example.com:/", "https://example.com:65536/", "https://example.com:abc/", "https://bad host/", "https://example.com/\n", "https://example.com\\evil/", "https://example.com/%no", "https://[wrong]/")]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertTrue(self.rpc(**overrides)["isError"])
        for field in ("appHandle", "windowId", "expectedUrl", "expectedText"):
            args = dict(self.args)
            args.pop(field)
            self.assertTrue(server.call_tool("quietbridge_wait_ready", args)["isError"])
        self.fake.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
