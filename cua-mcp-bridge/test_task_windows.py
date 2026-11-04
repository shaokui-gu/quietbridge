import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

# 运行态默认目录是真实用户目录；测试必须隔离。
os.environ.setdefault('CUA_CONFIG_DIR', tempfile.mkdtemp(prefix='cua-test-config-'))

import server
import browser_events

# 在 setUp 替换 TASK_FILE 之前保留真实解析结果。
RESOLVED_TASK_FILE = Path(server.TASK_FILE)


class TaskWindowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / "task-windows.json"
        self.shots = Path(self.directory.name).resolve() / "shots"
        self.shots.mkdir()
        self.state = {"app": "a", "apps": {"a": {"pid": 88, "name": "Chrome"}},
                      "window": {}, "snapshot": None, "snapshot_app": None, "snapshot_window": None}
        self.identity = {"pid": 88, "startedSeconds": "100", "startedMicroseconds": "10"}
        self.windows = [{"windowId": 7, "pid": 88, "isOnScreen": True},
                        {"windowId": 8, "pid": 88, "isOnScreen": True}]
        self.sessions = []
        self.observed_window = 7
        self.fake = mock.Mock(spec=server.HelperClient)
        self.fake.connection_id = "connection-1"
        self.fake._alive.return_value = True
        self.fake.request.side_effect = self.request
        for patcher in (mock.patch.object(server, "HELPER", self.fake),
                        mock.patch.object(server, "TASK_FILE", str(self.path)),
                        mock.patch.object(server, "SHOTS_DIR", str(self.shots)),
                        mock.patch.object(server, "TASK_BINDINGS", {}),
                        mock.patch.object(server, "TASK_DISPATCH", None),
                        mock.patch.object(server, "POWER_GUARD", {"active": False, "nextRenewAt": 0.0}),
                        mock.patch.object(server, "STATE", self.state),
                        mock.patch.object(server, "READ_ONLY", False),
                        mock.patch.object(server, "ALLOWLIST", []),
                        mock.patch.object(server.os.path, "isdir", return_value=False),
                        mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("禁止真实进程"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, method, params=None, **kwargs):
        if method == "browserSession":
            if params["action"] == "attach":
                session = {"sessionId": "native-%s" % len(self.sessions), "state": "attached",
                           "running": True, "pid": 88, "processIdentity": copy.deepcopy(self.identity),
                           "windowId": params["windowId"], "windows": copy.deepcopy(self.windows)}
                self.sessions.append(session)
                return copy.deepcopy(session)
            self.assertEqual(params["action"], "list", "不得 prepare/start/create")
            return {"sessions": copy.deepcopy(self.sessions)}
        if method == "listWindows":
            return copy.deepcopy(self.windows)
        if method == "preview":
            return {"action": params.get("action"), "windowId": params.get("windowId"),
                    "durationSeconds": params.get("durationSeconds"),
                    "preview": {"holding": params.get("action") == "hold", "visible": False,
                                "streamReady": params.get("action") == "hold", "mode": params.get("source"),
                                "trackingWindowId": params.get("windowId")}}
        if method == "powerGuard":
            return {"action": params.get("action"),
                    "guard": {"holding": params.get("action") == "hold",
                              "assertionType": "PreventUserIdleSystemSleep",
                              "remainingSeconds": params.get("durationSeconds", 0),
                              "limits": {"unlock": "not performed", "lidCloseSleep": "not prevented"}},
                    "privacy": {"active": params.get("action") == "hold",
                                "shieldVisible": params.get("action") == "hold",
                                "idleDisplaySleepPrevented": params.get("action") == "hold",
                                "credentials": "not requested or stored"}}
        if method in ("getState", "visualState", "screenshot"):
            return {"windowId": self.observed_window, "snapshotId": "snap-1", "tree": "- AXWindow",
                    "mimeType": "image/png", "data": "ZmFrZQ==", "bounds": {}}
        return {"dispatched": True}

    def rpc(self, name="cua_task", **args):
        if name == "cua_wait_ready":
            args = dict({"appHandle": "a", "expectedUrl": "https://example.com/", "expectedText": "x",
                         "timeoutSeconds": 0.1}, **args)
        return server.call_tool(name, args)

    def text(self, result):
        return result["content"][-1]["text"]

    def payload(self, **args):
        result = self.rpc(**args)
        self.assertFalse(result.get("isError"), self.text(result))
        return json.loads(self.text(result))

    def bind(self):
        task = self.payload(action="begin")
        return self.payload(action="bind", taskId=task["taskId"], appHandle="a", windowId=7, confirm=True)

    def error(self, code, name="cua_task", **args):
        result = self.rpc(name, **args)
        self.assertTrue(result.get("isError"), result)
        self.assertIn(code, self.text(result))

    def test_task_store_shares_the_browser_event_runtime_directory(self):
        # 两条入口若各算一份运行态，绑定会写进不同文件且互不可见。
        self.assertEqual(str(RESOLVED_TASK_FILE.parent), str(browser_events.CONFIG_DIR))
        self.assertEqual(RESOLVED_TASK_FILE.name, browser_events.TASK_FILE.name)

    def test_window_listing_stays_available_while_awaiting_bind(self):
        self.payload(action="begin")
        result = self.rpc(name="cua_windows", action="list", appHandle="a")
        self.assertFalse(result.get("isError"), self.text(result))
        # 只读枚举放行，输入仍必须被任务守卫拒绝。
        self.error("TASK_NOT_BOUND", "cua_click", appHandle="a", windowId=7, x=10, y=10)

    def test_begin_persists_uuid_and_waits_without_helper(self):
        first = self.payload(action="begin")
        second = self.payload(action="begin")
        self.assertNotEqual(first["taskId"], second["taskId"])
        self.assertEqual(first["state"], "awaiting_window")
        self.fake.request.assert_not_called()
        data = json.loads(self.path.read_text())
        self.assertEqual(data["activeTaskId"], second["taskId"])
        self.assertEqual(len(data["tasks"]), 2)
        self.error("TASK_NOT_BOUND", "cua_state", windowId=7)
        self.fake.request.assert_not_called()

    def test_bind_truthful_minimal_persistent_identity(self):
        bound = self.bind()
        self.assertEqual(bound["mode"], "attached_existing")
        self.assertFalse(bound["newWindowCreated"])
        self.assertEqual(bound["processIdentity"], self.identity)
        # 绑定的原生 attach 必须原样且只发一次；防睡眠断言是随后独立的尽力而为调用。
        attach = [c for c in self.fake.request.call_args_list if c.args[0] == "browserSession"]
        self.assertEqual([c.args for c in attach], [("browserSession", {
            "action": "attach", "pid": 88, "windowId": 7, "confirm": True})])
        record = json.loads(self.path.read_text())["tasks"][bound["taskId"]]
        self.assertEqual(set(record), {"taskId", "state", "mode", "newWindowCreated", "pid",
                                       "processIdentity", "windowId", "sessionId"})
        self.assertNotIn("appHandle", record)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_new_windows_order_and_focus_never_change_target(self):
        self.bind()
        self.windows.insert(0, {"windowId": 99, "pid": 88, "isOnScreen": True})
        self.state["window"]["a"] = 99
        result = self.rpc("cua_state", windowId=7)
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(self.state["snapshot_window"], 7)
        self.assertEqual(self.payload(action="status")["windowId"], 7)
        reads = [c for c in self.fake.request.call_args_list if c.args[0] == "getState"]
        self.assertTrue(all(c.args[1]["windowId"] == 7 for c in reads))
        self.assertTrue(all(c.kwargs["retry_safe"] is False for c in self.fake.request.call_args_list))

    def test_closed_window_invalidates_and_never_revives(self):
        self.bind()
        self.windows = [w for w in self.windows if w["windowId"] != 7]
        self.assertEqual(self.payload(action="status")["state"], "invalidated")
        self.assertFalse(server.POWER_GUARD["active"])
        releases = [c.args for c in self.fake.request.call_args_list if c.args[0] == "powerGuard"]
        self.assertEqual(releases[-1], ("powerGuard", {"action": "release"}))
        self.windows.append({"windowId": 7, "pid": 88})
        self.error("TASK_NOT_BOUND", "cua_state", windowId=7)
        self.error("TASK_INVALIDATED", action="bind", appHandle="a", windowId=7, confirm=True)

    def test_begin_replaces_a_bound_task_without_leaving_its_shield(self):
        self.bind()
        self.fake.request.reset_mock()
        replacement = self.payload(action="begin")
        self.assertEqual(replacement["state"], "awaiting_window")
        self.assertFalse(server.POWER_GUARD["active"])
        self.assertIn(("preview", {"action": "release"}),
                      [c.args for c in self.fake.request.call_args_list])
        self.assertIn(("powerGuard", {"action": "release"}),
                      [c.args for c in self.fake.request.call_args_list])

    def test_native_invalidation_and_pid_generation_change(self):
        for change in ("state", "identity", "missing_window"):
            with self.subTest(change=change):
                self.bind()
                session = self.sessions[-1]
                if change == "state":
                    session["state"] = "invalidated"
                elif change == "identity":
                    session["processIdentity"]["startedSeconds"] = "101"
                else:
                    session["windows"] = []
                self.assertEqual(self.payload(action="status")["state"], "invalidated")
                self.error("TASK_NOT_BOUND", "cua_click", windowId=7, x=1, y=2)

    def test_cross_connection_pauses_without_automatic_attach(self):
        self.bind()
        self.fake.request.reset_mock()
        self.fake.connection_id = "connection-2"
        self.assertEqual(self.payload(action="status")["state"], "requires_revalidation")
        self.error("REQUIRES_REVALIDATION", "cua_state", windowId=7)
        self.fake.request.assert_not_called()
        self.error("INVALID_ARGUMENT", action="bind", appHandle="a", windowId=7)
        self.assertEqual(self.payload(action="bind", appHandle="a", windowId=7, confirm=True)["state"], "bound")

    def test_bridge_restart_has_no_persisted_handle_authority(self):
        self.bind()
        server.TASK_BINDINGS.clear()
        self.fake.request.reset_mock()
        self.assertEqual(self.payload(action="status")["state"], "requires_revalidation")
        self.fake.request.assert_not_called()

    def test_explicit_revalidation_rejects_same_pid_new_generation(self):
        self.bind()
        self.fake.connection_id = "connection-2"
        self.identity["startedSeconds"] = "102"
        self.assertEqual(self.payload(action="bind", appHandle="a", windowId=7, confirm=True)["state"], "invalidated")
        self.error("TASK_NOT_BOUND", "cua_state", windowId=7)

    def test_mismatched_window_and_application_stop_before_observation_or_input(self):
        self.bind()
        for name in sorted(server.TASK_TARGET_TOOLS | {"cua_windows"}):
            with self.subTest(name=name):
                self.fake.request.reset_mock()
                self.error("TARGET_MISMATCH", name, windowId=8)
                self.error("TARGET_MISMATCH", name, appHandle="other", windowId=7)
                self.fake.request.assert_not_called()

    def test_missing_window_not_resolved_from_focus_or_cache(self):
        self.bind()
        self.state["window"]["a"] = 7
        for name in ("cua_state", "cua_visual_state", "cua_screenshot", "cua_click", "cua_hover", "cua_scroll", "cua_drag"):
            with self.subTest(name=name):
                self.error("TARGET_REQUIRED", name)
        self.assertFalse(any(c.args[0] in ("getState", "click", "hover", "scroll", "drag")
                             for c in self.fake.request.call_args_list))

    def test_snapshot_ownership_required_even_with_correct_window(self):
        self.bind()
        for app, window, snapshot in (("a", 8, "snap-1"), ("other", 7, "snap-1"), ("a", None, "snap-1")):
            self.state.update(snapshot=snapshot, snapshot_app=app, snapshot_window=window)
            for name in ("cua_click", "cua_hover", "cua_set_value"):
                self.error("TARGET_MISMATCH", name, elementIndex=1, value="x")
        self.state.update(snapshot="snap-1", snapshot_app="a", snapshot_window=7)
        self.error("TARGET_MISMATCH", "cua_scroll", windowId=7, snapshotId="unknown")
        for name in ("cua_click", "cua_hover", "cua_set_value"):
            result = self.rpc(name, elementIndex=1, value="x")
            self.assertFalse(result.get("isError"), result)
        self.assertTrue(any(c.args[0] == "hover" for c in self.fake.request.call_args_list))

    def test_keyboard_and_application_activation_always_denied(self):
        self.bind()
        for name in ("cua_type_text", "cua_paste", "cua_key"):
            self.error("UNSAFE_WINDOW_DELIVERY", name, windowId=7, text="x", key="a")
        self.error("UNSAFE_WINDOW_DELIVERY", "cua_windows", action="activate")
        self.error("TASK_SCOPE_REQUIRED", "cua_browser_session", action="start")
        self.error("TASK_SCOPE_REQUIRED", "cua_apps", action="launch")
        self.assertFalse(any(c.args[0] in ("typeText", "paste", "pressKey", "activate", "launchApp")
                             for c in self.fake.request.call_args_list))

    def test_wrong_observation_never_updates_snapshot(self):
        self.bind()
        self.observed_window = 8
        for name in ("cua_state", "cua_visual_state", "cua_screenshot", "cua_wait_ready"):
            self.error("TARGET_MISMATCH", name, windowId=7, **({"expectedText": "x"} if name == "cua_wait_ready" else {}))
            self.assertIsNone(self.state["snapshot"])

    def test_connection_loss_between_guard_and_native_send_is_not_replayed(self):
        self.bind()
        original = self.fake.request.side_effect
        def read(method, *args, **kwargs):
            result = original(method, *args, **kwargs)
            if method == "listWindows":
                self.fake.connection_id = "changed-during-check"
            return result
        self.fake.request.side_effect = read
        self.error("REQUIRES_REVALIDATION", "cua_state", windowId=7)
        self.assertFalse(any(c.args[0] == "getState" for c in self.fake.request.call_args_list))

    def test_atomic_replace_failure_preserves_original_and_cleans_temporary(self):
        self.payload(action="begin")
        original = self.path.read_bytes()
        with mock.patch.object(server.os, "replace", side_effect=OSError("模拟落盘失败")):
            self.error("TASK_STORE_ERROR", action="begin")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(Path(self.directory.name).glob("*.tmp")), [])

    def test_symlinks_file_lock_and_parent_are_rejected(self):
        target = Path(self.directory.name) / "target"
        target.write_text("不得修改")
        for name in (self.path, Path(str(self.path) + ".lock")):
            with self.subTest(name=name):
                if name.exists():
                    name.unlink()
                name.symlink_to(target)
                self.error("TASK_STORE_ERROR", action="begin")
                name.unlink()
                self.assertEqual(target.read_text(), "不得修改")
        actual = Path(self.directory.name) / "actual"
        actual.mkdir()
        link = Path(self.directory.name) / "linked"
        link.symlink_to(actual, target_is_directory=True)
        with mock.patch.object(server, "TASK_FILE", str(link / "task-windows.json")):
            self.error("TASK_STORE_ERROR", action="begin")

    def test_no_task_keyboard_requires_unique_target_and_read_resolution_unchanged(self):
        self.error("TARGET_REQUIRED", "cua_type_text", text="x")
        self.assertFalse(any(c.args[0] in ("typeText", "preview") for c in self.fake.request.call_args_list))
        self.windows = self.windows[:1]
        self.fake.request.reset_mock()
        result = self.rpc("cua_type_text", text="x")
        self.assertFalse(result.get("isError"), result)
        methods = [c.args[0] for c in self.fake.request.call_args_list]
        self.assertEqual(methods, ["listWindows", "preview", "typeText"])
        self.fake.request.reset_mock()
        result = self.rpc("cua_state")
        self.assertFalse(result.get("isError"), result)
        self.assertFalse(self.path.exists())

    def test_desktop_shared_before_coordinate_and_ax_inputs(self):
        self.bind()
        self.state.update(snapshot="snap-1", snapshot_app="a", snapshot_window=7)
        for name, args in (("cua_click", {"windowId": 7, "x": 1, "y": 2}),
                           ("cua_click", {"elementIndex": 1}),
                           ("cua_set_value", {"elementIndex": 1, "value": "private"})):
            self.fake.request.reset_mock()
            result = self.rpc(name, **args)
            self.assertFalse(result.get("isError"), result)
            calls = self.fake.request.call_args_list
            preview = next(i for i, c in enumerate(calls) if c.args[0] == "preview")
            input_index = next(i for i, c in enumerate(calls) if c.args[0] in server.INPUT_METHODS)
            self.assertLess(preview, input_index)
            self.assertEqual(calls[preview].args[1], {"action": "hold", "appHandle": "a", "windowId": 7,
                                                     "source": "desktop", "durationSeconds": 600})
            structured = result["structuredContent"]
            self.assertEqual(structured["execution"]["status"], "dispatched")
            self.assertNotIn("private", json.dumps(structured))

    def test_unready_desktop_never_dispatches_input(self):
        self.bind()
        original = self.request
        for field, value in (("streamReady", False), ("mode", "window"), ("trackingWindowId", 8)):
            def request(method, params=None, **kwargs):
                result = original(method, params, **kwargs)
                if method == "preview":
                    result["preview"][field] = value
                return result
            self.fake.request.side_effect = request
            self.fake.request.reset_mock()
            result = self.rpc("cua_click", windowId=7, x=1, y=2)
            self.assertTrue(result.get("isError"), result)
            self.assertEqual(result["structuredContent"]["execution"]["status"], "not_dispatched")
            self.assertFalse(any(c.args[0] in server.INPUT_METHODS for c in self.fake.request.call_args_list))

    def test_situation_is_bounded_read_only_and_explicit(self):
        self.bind()
        self.fake.request.reset_mock()
        result = self.rpc("cua_situation", appHandle="a", windowId=7)
        self.assertFalse(result.get("isError"), result)
        situation = result["structuredContent"]["situation"]
        self.assertEqual(situation["target"], {"appHandle": "a", "windowId": 7, "isOnScreen": True})
        self.assertEqual(situation["ax"]["truncated"], "unknown")
        self.assertFalse(any(c.args[0] in ("screenshot", "visualState") or
                             (c.args[0] == "preview" and c.args[1].get("action") != "status")
                             for c in self.fake.request.call_args_list))
        state = next(c.args[1] for c in self.fake.request.call_args_list if c.args[0] == "getState")
        self.assertEqual((state["maxElements"], state["maxDepth"]), (50, 6))
        self.error("TARGET_REQUIRED", "cua_situation", appHandle="a")

    def test_page_input_requires_bound_share_but_observe_does_not(self):
        bound = self.bind()
        with mock.patch.object(server, "_events_handler", return_value='{"status":"queued"}') as handler:
            self.fake.request.reset_mock()
            result = self.rpc(server._events_tool["name"], action="command", taskId=bound["taskId"], operation="click", ref=1)
            self.assertFalse(result.get("isError"), result)
            self.assertEqual(result["structuredContent"]["execution"]["status"], "queued")
            self.assertTrue(any(c.args[0] == "preview" for c in self.fake.request.call_args_list))
            handler.assert_called_once_with({"action": "command", "taskId": bound["taskId"], "operation": "click", "ref": 1})
            self.fake.request.reset_mock()
            self.rpc(server._events_tool["name"], action="command", taskId=bound["taskId"], operation="snapshot")
            self.assertFalse(any(c.args[0] == "preview" for c in self.fake.request.call_args_list))

    def test_observation_share_failure_and_read_only_stop_before_ax(self):
        self.bind()
        original = self.request
        def request(method, params=None, **kwargs):
            result = original(method, params, **kwargs)
            if method == "preview":
                result["preview"]["streamReady"] = False
            return result
        self.fake.request.side_effect = request
        for name in ("cua_state", "cua_visual_state", "cua_screenshot"):
            self.fake.request.reset_mock()
            result = self.rpc(name, windowId=7)
            self.assertEqual(result["structuredContent"]["errorCode"], "PREVIEW_NOT_READY")
            self.assertFalse(any(c.args[0] in ("getState", "visualState", "screenshot") for c in self.fake.request.call_args_list))
        with mock.patch.object(server, "READ_ONLY", True):
            result = self.rpc("cua_state", windowId=7)
            self.assertEqual(result["structuredContent"]["errorCode"], "SHARE_REQUIRED")

    def test_create_target_bound_shares_before_original_handler(self):
        bound = self.bind()
        self.fake.request.reset_mock()
        def handler(args):
            self.assertTrue(any(c.args[0] == "preview" and c.args[1].get("source") == "desktop"
                                and c.args[1].get("windowId") == 7 for c in self.fake.request.call_args_list))
            self.assertEqual(args, {"action": "create_target", "taskId": bound["taskId"]})
            return '{"status":"queued"}'
        with mock.patch.object(server, "_events_handler", side_effect=handler) as original:
            result = self.rpc(server._events_tool["name"], action="create_target", taskId=bound["taskId"])
            self.assertFalse(result.get("isError"), result)
            original.assert_called_once()

    def test_create_target_reports_missing_share_target_without_handler(self):
        awaiting = self.payload(action="begin")
        with mock.patch.object(server, "_events_handler") as handler:
            result = self.rpc(server._events_tool["name"], action="create_target", taskId=awaiting["taskId"])
            self.assertEqual(result["structuredContent"]["errorCode"], "TARGET_REQUIRED")
            handler.assert_not_called()

    def test_input_exception_is_unknown_and_never_replayed(self):
        self.bind()
        original = self.request
        def request(method, params=None, **kwargs):
            if method == "click":
                raise server.HelperError("HELPER_TIMEOUT", "unknown")
            return original(method, params, **kwargs)
        self.fake.request.side_effect = request
        result = self.rpc("cua_click", windowId=7, x=1, y=2)
        self.assertTrue(result.get("isError"), result)
        self.assertEqual(result["structuredContent"]["execution"]["status"], "unknown")
        self.assertEqual(sum(c.args[0] == "click" for c in self.fake.request.call_args_list), 1)
        self.assertFalse(result["structuredContent"]["retry"]["replaySideEffects"])

    def test_end_releases_task_guard_and_keeps_history(self):
        bound = self.bind()
        self.error("TARGET_MISMATCH", "cua_state", windowId=9)
        with self.assertRaises(Exception):
            self.payload(action="end", taskId=bound["taskId"])
        ended = self.payload(action="end", taskId=bound["taskId"], confirm=True)
        self.assertEqual(ended["state"], "ended")
        saved = json.loads(self.path.read_text())
        self.assertIsNone(saved["activeTaskId"])
        self.assertEqual(saved["tasks"][bound["taskId"]]["state"], "ended")
        # 结束只回到无任务原生模式，读取不再被任务守卫拦截。
        result = self.rpc("cua_state", appHandle="a", windowId=9)
        self.assertFalse(result.get("isError"), self.text(result))
        # 新任务仍可正常开始并绑定，历史记录不丢失。
        again = self.bind()
        self.assertEqual(again["state"], "bound")
        self.assertEqual(len(json.loads(self.path.read_text())["tasks"]), 2)

    def test_share_keeps_preview_for_the_task_and_unshare_releases_it(self):
        self.bind()
        shared = self.rpc("cua_windows", action="share", appHandle="a", windowId=7, durationSeconds=900)
        self.assertFalse(shared.get("isError"), self.text(shared))
        self.assertIn("holding=True", self.text(shared))
        previews = [c.args for c in self.fake.request.call_args_list if c.args[0] == "preview"]
        self.assertEqual(previews[-1], ("preview", {"action": "hold", "appHandle": "a",
                                                   "windowId": 7, "durationSeconds": 900,
                                                   "source": "window"}))
        released = self.rpc("cua_windows", action="unshare", appHandle="a")
        self.assertFalse(released.get("isError"), self.text(released))
        previews = [c.args for c in self.fake.request.call_args_list if c.args[0] == "preview"]
        self.assertEqual(previews[-1], ("preview", {"action": "release"}))

    def test_share_can_cover_the_whole_desktop(self):
        self.bind()
        result = self.rpc("cua_windows", action="share", appHandle="a", windowId=7, source="desktop")
        self.assertFalse(result.get("isError"), self.text(result))
        self.assertIn("整个桌面", self.text(result))
        sent = [c.args for c in self.fake.request.call_args_list if c.args[0] == "preview"][-1][1]
        self.assertEqual(sent["source"], "desktop")
        self.error("INVALID_ARGUMENT", "cua_windows", action="share", appHandle="a", windowId=7, source="screen")

    def test_share_never_retargets_the_bound_window_or_accepts_a_bad_lease(self):
        self.bind()
        self.error("TARGET_MISMATCH", "cua_windows", action="share", appHandle="a", windowId=8)
        for duration in (5, 4000, "600", True):
            self.error("INVALID_ARGUMENT", "cua_windows", action="share", appHandle="a",
                       windowId=7, durationSeconds=duration)
        self.error("INVALID_ARGUMENT", "cua_windows", action="share", appHandle="a")
        self.assertFalse(any(c.args[0] == "preview" for c in self.fake.request.call_args_list))

    def test_share_requires_a_bound_task(self):
        self.payload(action="begin")
        self.error("TASK_NOT_BOUND", "cua_windows", action="share", appHandle="a", windowId=7)

    def test_end_releases_the_share_lease_after_the_task(self):
        bound = self.bind()
        self.rpc("cua_windows", action="share", appHandle="a", windowId=7)
        self.fake.request.reset_mock()
        ended = self.payload(action="end", taskId=bound["taskId"], confirm=True)
        self.assertEqual(ended["state"], "ended")
        self.assertEqual([c.args for c in self.fake.request.call_args_list if c.args[0] == "preview"],
                         [("preview", {"action": "release"})])

    def test_share_falls_back_to_target_window_on_background_space(self):
        calls = []
        def request_share(app, window, source):
            calls.append(source)
            return ('connection-1', {'streamReady': True, 'mode': source, 'trackingWindowId': window,
                                     'scope': 'display_current_space' if source == 'desktop' else 'window',
                                     'targetOnCurrentSpace': False if source == 'desktop' else None})
        with mock.patch.object(server, 'request_share', side_effect=request_share), \
                mock.patch.object(server, 'CALL_RECORDS', []), mock.patch.object(server, 'TASK_DISPATCH', None):
            server.ensure_desktop('a', 7)
            self.assertEqual(calls, ['desktop', 'window'])
            self.assertEqual(server.SHARE_RECORD['source'], 'window')
            self.assertIn('background_space', server.SHARE_RECORD['reason'])

    def test_launch_waits_for_unique_window_and_never_guesses_multiple(self):
        with mock.patch.object(server, 'list_windows', side_effect=[[], [{'windowId': 5, 'pid': 8, 'isOnScreen': True, 'title': '主窗'}]]), \
                mock.patch.object(server, 'time', mock.Mock(monotonic=lambda: 0, sleep=lambda _: None)):
            self.assertEqual(server.wait_for_window('a', 8), (5, 'ok'))
        with mock.patch.object(server, 'list_windows', return_value=[{'windowId': 5, 'pid': 8, 'isOnScreen': True},
                                                                     {'windowId': 6, 'pid': 8, 'isOnScreen': True}]), \
                mock.patch.object(server, 'time', mock.Mock(monotonic=lambda: 0, sleep=lambda _: None)):
            window, reason = server.wait_for_window('a', 8)
            self.assertIsNone(window)
            self.assertIn('多个候选', reason)

    def test_unbound_create_target_shares_only_the_unique_new_window(self):
        with mock.patch.object(server, 'browser_window_snapshot', return_value=('a', {1, 2})), \
                mock.patch.object(server, '_events_handler', return_value='{"status":"queued"}'), \
                mock.patch.object(server, 'share_new_window') as shared, \
                mock.patch.object(server, 'task_store', mock.MagicMock(return_value=mock.MagicMock(
                    __enter__=lambda s: {'tasks': {'t': {'windowId': None}}, 'activeTaskId': 't'},
                    __exit__=lambda *args: None))):
            self.assertEqual(server._events_dispatch({'action': 'create_target', 'taskId': 't'}), '{"status":"queued"}')
            shared.assert_called_once_with('a', {1, 2})

    def test_unbound_create_reports_share_pending_without_claiming_success(self):
        with mock.patch.object(server, 'browser_window_snapshot', return_value=('a', set())), \
                mock.patch.object(server, '_events_handler', return_value='{"status":"queued"}'), \
                mock.patch.object(server, 'share_new_window', side_effect=server.HelperError('PREVIEW_NOT_READY', 'no window')), \
                mock.patch.object(server, 'task_store', mock.MagicMock(return_value=mock.MagicMock(
                    __enter__=lambda s: {'tasks': {'t': {'windowId': None}}, 'activeTaskId': 't'},
                    __exit__=lambda *args: None))):
            output = server._events_dispatch({'action': 'create_target', 'taskId': 't'})
            self.assertIn('共享待补', output)
            self.assertIn('PREVIEW_NOT_READY', output)

    def test_lost_persistent_record_never_disables_guard(self):
        self.bind()
        self.path.unlink()
        self.fake.request.reset_mock()
        self.error("TASK_STORE_ERROR", "cua_state", windowId=8)
        self.fake.request.assert_not_called()

    def test_native_send_rechecks_connection_before_start_or_write(self):
        self.bind()
        client = server.HelperClient("/not-executed")
        client.proc = mock.Mock()
        client.proc.poll.return_value = None
        client.connection_id = "original"
        with mock.patch.object(server, "HELPER", client):
            context = {"connection": server.current_connection(), "appHandle": "a", "windowId": 7}
            with mock.patch.object(server, "TASK_DISPATCH", context), mock.patch.object(client, "_start") as start:
                client.connection_id = "replacement"
                with self.assertRaises(server.HelperError) as raised:
                    client._request_once("click", {"appHandle": "a", "windowId": 7}, 1)
                self.assertEqual(raised.exception.code, "REQUIRES_REVALIDATION")
                start.assert_not_called()
                client.proc.stdin.write.assert_not_called()

    def test_allowed_coordinate_tools_remain_explicitly_bound(self):
        self.bind()
        cases = (("cua_click", {"x": 1, "y": 2}), ("cua_hover", {"x": 1, "y": 2}),
                 ("cua_scroll", {"x": 1, "y": 2}),
                 ("cua_drag", {"fromX": 1, "fromY": 2, "toX": 3, "toY": 4}),
                 ("cua_visual_state", {}), ("cua_wait_ready", {"expectedText": "x"}))
        for name, args in cases:
            with self.subTest(name=name):
                result = self.rpc(name, windowId=7, **args)
                self.assertFalse(result.get("isError"), result)
        for call in self.fake.request.call_args_list:
            if call.args[0] in ("click", "hover", "scroll", "drag", "visualState", "getState"):
                self.assertEqual(call.args[1]["windowId"], 7)
                self.assertEqual(call.args[1]["appHandle"], "a")

    def test_click_path_and_focus_restoration_are_visible_without_changing_verification(self):
        self.bind()
        evidence = {"dispatched": True, "verified": False, "route": "accessibility",
                    "axAction": "AXPress", "focusRestorationAttempted": True,
                    "focusRestorationAccepted": True, "frontPidBefore": 88, "frontPidAfterAction": 99}
        with mock.patch.object(server, "call_pointer", return_value=evidence):
            reply = self.rpc("cua_click", windowId=7, x=1, y=2)
        self.assertFalse(reply.get("isError"), reply)
        text = self.text(reply)
        self.assertIn("route=accessibility", text)
        self.assertIn("axAction=AXPress", text)
        self.assertIn("focusRestorationAttempted=True", text)
        self.assertIn("frontPid=88->99", text)
        self.assertIn("verified=False", text, "恢复焦点不能冒充动作复核成功")

    def test_wait_budget_includes_guard_and_preserves_target_on_every_poll(self):
        self.bind()
        self.fake.request.reset_mock()
        now = [100.0]
        original = self.fake.request.side_effect
        waits = []
        def read(method, params=None, **kwargs):
            if method in ("browserSession", "listWindows"):
                now[0] += 1
            if method == "getState":
                return {"windowId": 7, "appHandle": "a", "pid": 88, "snapshotId": "ax-1",
                        "tree": '- AXWebArea url="https://example.com/"\n  - AXStaticText "x"'}
            return original(method, params, **kwargs)
        def wait(seconds):
            waits.append(seconds)
            now[0] += seconds
        self.fake.request.side_effect = read
        with mock.patch.object(server.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(server.threading.Event, "wait", side_effect=wait):
            result = self.rpc("cua_wait_ready", windowId=7, timeoutSeconds=5, intervalSeconds=1)
        self.assertFalse(result.get("isError"), result)
        self.assertTrue(json.loads(self.text(result).split("\n")[0])["ready"])
        self.assertEqual([c.kwargs["timeout"] for c in self.fake.request.call_args_list], [5, 4, 3, 3, 2])
        shares = [c for c in self.fake.request.call_args_list if c.args[0] == "preview"]
        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0].args[1]["source"], "desktop")
        self.assertEqual(waits, [1])
        self.assertTrue(all(c.kwargs["retry_safe"] is False for c in self.fake.request.call_args_list))
        self.assertIsNone(server.TASK_DISPATCH)
        self.assertIsNone(server.WAIT_DEADLINE)

    def test_wait_connection_loss_during_interval_is_not_retried(self):
        self.bind()
        original = self.fake.request.side_effect
        def read(method, params=None, **kwargs):
            if method == "getState":
                return {"windowId": 7, "appHandle": "a", "pid": 88, "snapshotId": "ax-1",
                        "tree": '- AXWebArea url="https://example.com/"\n  - AXStaticText "x"'}
            return original(method, params, **kwargs)
        self.fake.request.side_effect = read
        now = [100.0]
        def wait(seconds):
            now[0] += seconds
            self.fake.connection_id = "changed-during-wait"
        with mock.patch.object(server.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(server.threading.Event, "wait", side_effect=wait):
            self.error("REQUIRES_REVALIDATION", "cua_wait_ready", windowId=7, timeoutSeconds=5, intervalSeconds=1)
        self.assertEqual(sum(c.args[0] == "getState" for c in self.fake.request.call_args_list), 1)

    def test_bind_confirmation_readonly_and_incomplete_native_response(self):
        self.payload(action="begin")
        self.error("INVALID_ARGUMENT", action="bind", appHandle="a", windowId=7, confirm=1)
        with mock.patch.object(server, "READ_ONLY", True):
            self.error("READ_ONLY", action="bind", appHandle="a", windowId=7, confirm=True)
        self.fake.request.assert_not_called()
        self.fake.request.side_effect = None
        self.fake.request.return_value = {"state": "attached"}
        self.error("ATTACH_UNVERIFIED", action="bind", appHandle="a", windowId=7, confirm=True)
        self.assertEqual(json.loads(self.path.read_text())["tasks"][json.loads(self.path.read_text())["activeTaskId"]]["state"], "awaiting_window")

    def test_bind_holds_power_guard_and_end_releases_it(self):
        # 锁屏不会终止进程，空闲睡眠才会；断言必须跟着任务租约出现和消失。
        bound = self.bind()
        self.assertTrue(bound["powerGuard"]["holding"])
        self.assertTrue(bound["powerGuard"]["privacy"]["active"])
        self.assertTrue(bound["powerGuard"]["privacy"]["shieldVisible"])
        holds = [c.args for c in self.fake.request.call_args_list if c.args[0] == "powerGuard"]
        self.assertEqual(holds, [("powerGuard", {"action": "hold",
                                                 "durationSeconds": server.POWER_GUARD_SECONDS})])
        self.assertTrue(server.POWER_GUARD["active"])

        ended = self.payload(action="end", confirm=True)
        self.assertFalse(ended["powerGuard"]["holding"])
        self.assertFalse(ended["powerGuard"]["privacy"]["active"])
        self.assertFalse(server.POWER_GUARD["active"])
        actions = [c.args[1]["action"] for c in self.fake.request.call_args_list if c.args[0] == "powerGuard"]
        self.assertEqual(actions, ["hold", "release"])

    def test_power_guard_failure_never_blocks_the_task(self):
        # 断言只是尽力而为的保护，拿不到也不能让任务本身失败。
        original = self.request

        def failing(method, params=None, **kwargs):
            if method == "powerGuard":
                raise server.HelperError("HELPER_TIMEOUT", "boom")
            return original(method, params, **kwargs)

        self.fake.request.side_effect = failing
        bound = self.bind()
        self.assertEqual(bound["state"], "bound")
        self.assertEqual(bound["powerGuard"], {})
        self.assertFalse(server.POWER_GUARD["active"])


if __name__ == "__main__":
    unittest.main()
