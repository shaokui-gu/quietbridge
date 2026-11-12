import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

# 运行态默认目录是真实用户目录；测试必须隔离。
os.environ.setdefault('QUIETBRIDGE_CONFIG_DIR', tempfile.mkdtemp(prefix='quietbridge-test-config-'))

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
        owner = server.TaskOwner()
        for patcher in (mock.patch.object(server, "HELPER", self.fake),
                        mock.patch.object(server, "TASK_OWNER", owner),
                        mock.patch.object(server, 'control_snapshot', return_value={"master": True}),
                        mock.patch.object(server, "TASK_FILE", str(self.path)),
                        mock.patch.object(server, "SHOTS_DIR", str(self.shots)),
                        mock.patch.object(server, "TASK_BINDINGS", {}),
                        mock.patch.object(server, "TASK_DISPATCH", None),
                        mock.patch.object(server, "SHARE_SELECTION", None),
                        mock.patch.object(server, "POWER_GUARD", {"active": False, "nextRenewAt": 0.0}),
                        mock.patch.object(server, "SESSION_PROTECTION", server.ProtectionSession()),
                        mock.patch.object(server, "STATE", self.state),
                        mock.patch.object(server, "READ_ONLY", False),
                        mock.patch.object(server, "ALLOWLIST", []),
                        mock.patch.object(server.subprocess, "Popen", side_effect=AssertionError("禁止真实进程"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(owner.close)

    def request(self, method, params=None, **kwargs):
        if method == "windowIdentity":
            window = next((w for w in self.windows if w['windowId'] == params['windowId']), None)
            if not window:
                raise server.HelperError('WINDOW_UNAVAILABLE', 'fixture window closed')
            return {'pid': 88, 'windowId': window['windowId'], 'processIdentity': copy.deepcopy(self.identity)}
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
                                "trackingWindowId": params.get("windowId"), "targetOnCurrentSpace": True}}
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

    def rpc(self, name="quietbridge_task", **args):
        if name == "quietbridge_wait_ready":
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
        task = self.payload(action="begin", interactionMode="desktop")
        return self.payload(action="bind", taskId=task["taskId"], appHandle="a", windowId=7, confirm=True)

    def acknowledge(self, task, session='session-a', request='run-a'):
        from adapters.workbuddy.lifecycle import handle_hook
        handle_hook({'session_id': session, 'hook_event_name': 'PostToolUse',
                     'tool_name': 'quietbridge_task', 'tool_input': {'action': 'begin'},
                     'tool_response': task}, self.path.parent, request)

    def test_background_is_default_and_all_native_input_routes_stop_before_dispatch(self):
        task = self.payload(action="begin")
        self.assertEqual(task['interactionMode'], 'background_browser')
        self.payload(action="bind", taskId=task['taskId'], appHandle="a", windowId=7, confirm=True)
        self.fake.request.reset_mock()
        for name in ('quietbridge_click', 'quietbridge_hover', 'quietbridge_scroll', 'quietbridge_drag', 'quietbridge_type_text',
                     'quietbridge_paste', 'quietbridge_key', 'quietbridge_set_value'):
            with self.subTest(name=name):
                self.error('BACKGROUND_ONLY', name, appHandle='a', windowId=7)
        self.error('BACKGROUND_ONLY', 'quietbridge_windows', action='activate', appHandle='a', windowId=7)
        self.fake.request.assert_not_called()

    def test_background_mode_cannot_be_downgraded_by_bind(self):
        task = self.payload(action='begin')
        self.error('INVALID_ARGUMENT', action='bind', taskId=task['taskId'],
                   appHandle='a', windowId=7, confirm=True, interactionMode='desktop')
        self.assertEqual(json.loads(self.path.read_text())['tasks'][task['taskId']]['interactionMode'],
                         'background_browser')

    def test_background_browser_input_survives_unavailable_native_preview(self):
        task = self.payload(action='begin')
        self.payload(action='bind', taskId=task['taskId'], appHandle='a', windowId=7, confirm=True)
        with mock.patch.object(server, 'ensure_background_preview', side_effect=server.HelperError('CAPTURE_BUSY', 'busy')), \
                mock.patch.object(server, 'native_preview_matches', return_value=True), \
                mock.patch.object(server, '_events_handler', return_value='{"status":"queued"}') as handler:
            self.fake.request.reset_mock()
            result = self.rpc('quietbridge_browser_events', action='command', taskId=task['taskId'],
                              operation='click', ref=1, snapshotId='snap')
            self.assertFalse(result.get('isError'), result)
            handler.assert_called_once()
            self.assertFalse(any(c.args[0] in ('click', 'activate', 'pressKey', 'typeText')
                                 for c in self.fake.request.call_args_list))

    def test_background_preview_does_not_display_an_unverified_native_window(self):
        task = self.payload(action='begin')
        self.payload(action='bind', taskId=task['taskId'], appHandle='a', windowId=7, confirm=True)
        with mock.patch.object(server, 'native_preview_matches', return_value=False), \
                mock.patch.object(server, 'ensure_background_preview') as preview, \
                mock.patch.object(server, '_events_handler', return_value='{"status":"queued"}'):
            result = self.rpc('quietbridge_browser_events', action='command', taskId=task['taskId'],
                              operation='click', ref=1, snapshotId='snap')
            self.assertFalse(result.get('isError'), result)
            preview.assert_not_called()
            self.error('BROWSER_MAPPING_REQUIRED', 'quietbridge_windows', action='share', appHandle='a', windowId=7)

    def test_background_preview_does_not_wait_for_a_video_frame(self):
        original = self.request
        def pending_frame(method, params=None, **kwargs):
            result = original(method, params, **kwargs)
            if method == 'preview':
                result['preview']['streamReady'] = False
            return result
        self.fake.request.side_effect = pending_frame
        with mock.patch.object(server, 'TASK_DISPATCH', None):
            self.assertEqual(server.ensure_background_preview('a', 7), server.current_connection())
        params = self.fake.request.call_args.args[1]
        self.assertIs(params['waitForFrame'], False)
        self.assertEqual(params['source'], 'application')

    def test_browser_page_commands_preserve_explicit_system_share_scope(self):
        for source in ('application', 'desktop'):
            with self.subTest(source=source):
                server.SHARE_SELECTION = dict(appHandle='a', windowId=7,
                                              connection=server.current_connection(), source=source)
                with mock.patch.object(server, 'request_share', return_value=(server.current_connection(),
                            {'streamReady': True, 'targetOnCurrentSpace': True})) as share:
                    server.ensure_background_preview('a', 7)
                self.assertEqual(share.call_args.args[2], source)

    def test_share_without_source_can_choose_browser_content(self):
        self.bind()
        reply = server.tool_windows(dict(action='share', appHandle='a', windowId=7))
        self.assertIn(server.SHARE_RECORD['source'], ('application', 'window'))
        self.assertNotIn('定时帧', reply)

    def error(self, code, name="quietbridge_task", **args):
        result = self.rpc(name, **args)
        self.assertTrue(result.get("isError"), result)
        self.assertIn(code, self.text(result))

    def test_task_store_shares_the_browser_event_runtime_directory(self):
        # 两条入口若各算一份运行态，绑定会写进不同文件且互不可见。
        self.assertEqual(str(RESOLVED_TASK_FILE.parent), str(browser_events.CONFIG_DIR))
        self.assertEqual(RESOLVED_TASK_FILE.name, browser_events.TASK_FILE.name)

    def test_window_listing_stays_available_while_awaiting_bind(self):
        self.payload(action="begin", interactionMode="desktop")
        result = self.rpc(name="quietbridge_windows", action="list", appHandle="a")
        self.assertFalse(result.get("isError"), self.text(result))
        # 只读枚举放行，输入仍必须被任务守卫拒绝。
        self.error("TASK_NOT_BOUND", "quietbridge_click", appHandle="a", windowId=7, x=10, y=10)

    def test_begin_persists_uuid_and_waits_without_helper(self):
        first = self.payload(action="begin", interactionMode="desktop")
        second = self.payload(action="begin", interactionMode="desktop")
        self.assertNotEqual(first["taskId"], second["taskId"])
        self.assertEqual(first["state"], "awaiting_window")
        self.fake.request.assert_not_called()
        data = json.loads(self.path.read_text())
        self.assertEqual(data["activeTaskId"], second["taskId"])
        self.assertEqual(len(data["tasks"]), 2)
        self.error("TASK_NOT_BOUND", "quietbridge_state", windowId=7)
        self.fake.request.assert_not_called()

    def test_bind_truthful_minimal_persistent_identity(self):
        bound = self.bind()
        self.assertEqual(bound["mode"], "attached_existing")
        self.assertFalse(bound["newWindowCreated"])
        self.assertEqual(bound["processIdentity"], self.identity)
        # 桌面窗口使用通用进程代际证明，不能进入 Chrome-only attach。
        attach = [c for c in self.fake.request.call_args_list if c.args[0] == "browserSession"]
        self.assertEqual(attach, [])
        self.assertIn(("windowIdentity", {"appHandle": "a", "windowId": 7}),
                      [c.args for c in self.fake.request.call_args_list])
        record = json.loads(self.path.read_text())["tasks"][bound["taskId"]]
        self.assertEqual(set(record), {"taskId", "state", "mode", "newWindowCreated", "pid",
                                       "processIdentity", "windowId", "interactionMode", "lease"})
        self.assertNotIn("appHandle", record)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_new_windows_order_and_focus_never_change_target(self):
        self.bind()
        self.windows.insert(0, {"windowId": 99, "pid": 88, "isOnScreen": True})
        self.state["window"]["a"] = 99
        result = self.rpc("quietbridge_state", windowId=7)
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
        self.assertTrue(server.POWER_GUARD["active"])
        releases = [c.args for c in self.fake.request.call_args_list if c.args[0] == "powerGuard"]
        self.assertTrue(all(c[1]['action'] != 'release' for c in releases))
        self.assertIn(('preview', {'action': 'release'}), [c.args for c in self.fake.request.call_args_list])
        self.windows.append({"windowId": 7, "pid": 88})
        self.error("TASK_NOT_BOUND", "quietbridge_state", windowId=7)
        self.error("TASK_INVALIDATED", action="bind", appHandle="a", windowId=7, confirm=True)

    def test_replacing_a_phase_releases_preview_but_retains_session_protection(self):
        self.bind()
        self.fake.request.reset_mock()
        replacement = self.payload(action="begin", interactionMode="desktop")
        self.assertEqual(replacement["state"], "awaiting_window")
        self.assertTrue(server.POWER_GUARD["active"])
        self.assertIn(("preview", {"action": "release"}),
                      [c.args for c in self.fake.request.call_args_list])
        self.assertNotIn(("powerGuard", {"action": "release"}),
                         [c.args for c in self.fake.request.call_args_list])

    def test_native_invalidation_and_pid_generation_change(self):
        for change in ("state", "identity", "missing_window"):
            with self.subTest(change=change):
                task = self.payload(action='begin')
                self.payload(action='bind', taskId=task['taskId'], appHandle='a', windowId=7, confirm=True)
                session = self.sessions[-1]
                if change == "state":
                    session["state"] = "invalidated"
                elif change == "identity":
                    session["processIdentity"]["startedSeconds"] = "101"
                else:
                    session["windows"] = []
                self.assertEqual(self.payload(action="status")["state"], "invalidated")
                self.error("TASK_NOT_BOUND", "quietbridge_state", windowId=7)

    def test_desktop_binds_non_chrome_and_rejects_a_reused_process_generation(self):
        self.state['apps']['a']['name'] = 'Calculator'
        self.state['apps']['a']['bundleId'] = 'com.apple.calculator'
        with mock.patch.object(server, 'ALLOWLIST', ['com.apple.calculator']):
            bound = self.bind()
            self.assertEqual(bound['state'], 'bound')
            self.assertNotIn('sessionId', bound)
            self.assertFalse(any(c.args[0] == 'browserSession' for c in self.fake.request.call_args_list))
            self.assertEqual(self.payload(action='status')['state'], 'bound')
            self.identity['startedSeconds'] = '101'
            self.assertEqual(self.payload(action='status')['state'], 'invalidated')
            self.assertTrue(server.POWER_GUARD['active'])
            self.error('TASK_NOT_BOUND', 'quietbridge_click', windowId=7, x=1, y=2)

    def test_terminated_desktop_application_closes_preview_and_retains_session(self):
        self.bind()
        original = self.fake.request.side_effect
        def gone(method, params=None, **kwargs):
            if method == 'windowIdentity':
                raise server.HelperError('APP_UNAVAILABLE', 'fixture process terminated')
            return original(method, params, **kwargs)
        self.fake.request.side_effect = gone
        self.assertEqual(self.payload(action='status')['state'], 'invalidated')
        self.assertTrue(server.POWER_GUARD['active'])

    def test_cross_connection_pauses_without_automatic_attach(self):
        self.bind()
        self.fake.request.reset_mock()
        self.fake.connection_id = "connection-2"
        self.assertEqual(self.payload(action="status")["state"], "requires_revalidation")
        self.error("REQUIRES_REVALIDATION", "quietbridge_state", windowId=7)
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
        self.error("TASK_NOT_BOUND", "quietbridge_state", windowId=7)

    def test_mismatched_window_and_application_stop_before_observation_or_input(self):
        self.bind()
        for name in sorted(server.TASK_TARGET_TOOLS | {"quietbridge_windows"}):
            with self.subTest(name=name):
                self.fake.request.reset_mock()
                self.error("TARGET_MISMATCH", name, windowId=8)
                self.error("TARGET_MISMATCH", name, appHandle="other", windowId=7)
                self.fake.request.assert_not_called()

    def test_missing_window_not_resolved_from_focus_or_cache(self):
        self.bind()
        self.state["window"]["a"] = 7
        for name in ("quietbridge_state", "quietbridge_visual_state", "quietbridge_screenshot", "quietbridge_click", "quietbridge_hover", "quietbridge_scroll", "quietbridge_drag"):
            with self.subTest(name=name):
                self.error("TARGET_REQUIRED", name)
        self.assertFalse(any(c.args[0] in ("getState", "click", "hover", "scroll", "drag")
                             for c in self.fake.request.call_args_list))

    def test_snapshot_ownership_required_even_with_correct_window(self):
        self.bind()
        for app, window, snapshot in (("a", 8, "snap-1"), ("other", 7, "snap-1"), ("a", None, "snap-1")):
            self.state.update(snapshot=snapshot, snapshot_app=app, snapshot_window=window)
            for name in ("quietbridge_click", "quietbridge_hover", "quietbridge_set_value"):
                self.error("TARGET_MISMATCH", name, elementIndex=1, value="x")
        self.state.update(snapshot="snap-1", snapshot_app="a", snapshot_window=7)
        self.error("STALE_SNAPSHOT", "quietbridge_scroll", windowId=7, snapshotId="unknown")
        for name in ("quietbridge_click", "quietbridge_hover", "quietbridge_set_value"):
            result = self.rpc(name, elementIndex=1, value="x")
            self.assertFalse(result.get("isError"), result)
        self.assertTrue(any(c.args[0] == "hover" for c in self.fake.request.call_args_list))

    def test_keyboard_and_application_activation_always_denied(self):
        self.bind()
        for name in ("quietbridge_type_text", "quietbridge_paste", "quietbridge_key"):
            self.error("UNSAFE_WINDOW_DELIVERY", name, windowId=7, text="x", key="a")
        self.error("UNSAFE_WINDOW_DELIVERY", "quietbridge_windows", action="activate")
        self.error("TASK_SCOPE_REQUIRED", "quietbridge_browser_session", action="start")
        self.error("TASK_SCOPE_REQUIRED", "quietbridge_apps", action="launch")
        self.assertFalse(any(c.args[0] in ("typeText", "paste", "pressKey", "activate", "launchApp")
                             for c in self.fake.request.call_args_list))

    def test_browser_preparation_is_allowed_before_binding_but_never_migrates_a_bound_task(self):
        task = self.payload(action="begin", interactionMode="background_browser")
        valid = {"action": "start", "sessionId": "12345678-1234-1234-1234-123456789ABC", "confirm": True}
        with mock.patch.object(server, "tool_browser_session", return_value={"content": [{"type": "text", "text": "{}"}]}):
            args, context = server.task_guard("quietbridge_browser_session", valid)
            self.assertEqual(args, valid)
            self.assertIsNone(context)
        self.payload(action="bind", taskId=task["taskId"], appHandle="a", windowId=7, confirm=True)
        for action in ("prepare", "list"):
            self.assertEqual(server.task_guard("quietbridge_browser_session", {"action": action})[0], {"action": action})
        self.fake.request.reset_mock()
        self.error("TASK_SCOPE_REQUIRED", "quietbridge_browser_session", **valid)
        self.fake.request.assert_not_called()

    def test_wrong_observation_never_updates_snapshot(self):
        self.bind()
        self.observed_window = 8
        for name in ("quietbridge_state", "quietbridge_visual_state", "quietbridge_screenshot", "quietbridge_wait_ready"):
            self.error("TARGET_MISMATCH", name, windowId=7, **({"expectedText": "x"} if name == "quietbridge_wait_ready" else {}))
            self.assertIsNone(self.state["snapshot"])

    def test_connection_loss_between_guard_and_native_send_is_not_replayed(self):
        self.bind()
        original = self.fake.request.side_effect
        def read(method, *args, **kwargs):
            result = original(method, *args, **kwargs)
            if method == "windowIdentity":
                self.fake.connection_id = "changed-during-check"
            return result
        self.fake.request.side_effect = read
        self.error("REQUIRES_REVALIDATION", "quietbridge_state", windowId=7)
        self.assertFalse(any(c.args[0] == "getState" for c in self.fake.request.call_args_list))

    def test_atomic_replace_failure_preserves_original_and_cleans_temporary(self):
        self.payload(action="begin", interactionMode="desktop")
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
        self.error("TARGET_REQUIRED", "quietbridge_type_text", text="x")
        self.assertFalse(any(c.args[0] in ("typeText", "preview") for c in self.fake.request.call_args_list))
        self.windows = self.windows[:1]
        self.fake.request.reset_mock()
        result = self.rpc("quietbridge_type_text", text="x")
        self.assertFalse(result.get("isError"), result)
        methods = [c.args[0] for c in self.fake.request.call_args_list]
        self.assertEqual(methods, ["listWindows", "preview", "typeText"])
        self.fake.request.reset_mock()
        result = self.rpc("quietbridge_state")
        self.assertFalse(result.get("isError"), result)
        self.assertFalse(self.path.exists())

    def test_application_shared_before_coordinate_and_ax_inputs(self):
        self.bind()
        self.state.update(snapshot="snap-1", snapshot_app="a", snapshot_window=7)
        for name, args in (("quietbridge_click", {"windowId": 7, "x": 1, "y": 2}),
                           ("quietbridge_click", {"elementIndex": 1}),
                           ("quietbridge_set_value", {"elementIndex": 1, "value": "private"})):
            self.fake.request.reset_mock()
            result = self.rpc(name, **args)
            self.assertFalse(result.get("isError"), result)
            calls = self.fake.request.call_args_list
            preview = next(i for i, c in enumerate(calls) if c.args[0] == "preview")
            input_index = next(i for i, c in enumerate(calls) if c.args[0] in server.INPUT_METHODS)
            self.assertLess(preview, input_index)
            self.assertEqual(calls[preview].args[1], {"action": "hold", "appHandle": "a", "windowId": 7,
                                                     "source": "application", "durationSeconds": 600})
            structured = result["structuredContent"]
            self.assertEqual(structured["execution"]["status"], "dispatched")
            self.assertNotIn("private", json.dumps(structured))

    def test_unready_application_never_dispatches_input(self):
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
            result = self.rpc("quietbridge_click", windowId=7, x=1, y=2)
            self.assertTrue(result.get("isError"), result)
            self.assertEqual(result["structuredContent"]["execution"]["status"], "not_dispatched")
            self.assertFalse(any(c.args[0] in server.INPUT_METHODS for c in self.fake.request.call_args_list))

    def test_situation_is_bounded_read_only_and_explicit(self):
        self.bind()
        self.fake.request.reset_mock()
        result = self.rpc("quietbridge_situation", appHandle="a", windowId=7)
        self.assertFalse(result.get("isError"), result)
        situation = result["structuredContent"]["situation"]
        self.assertEqual(situation["target"], {"appHandle": "a", "windowId": 7, "isOnScreen": True})
        self.assertEqual(situation["ax"]["truncated"], "unknown")
        readable = json.loads(self.text(result).split("\n", 1)[1])
        self.assertEqual(readable, situation)
        self.assertEqual(readable["ax"]["tree"], "- AXWindow")
        self.assertFalse(readable["preview"]["holding"])
        self.assertFalse(any(c.args[0] in ("screenshot", "visualState") or
                             (c.args[0] == "preview" and c.args[1].get("action") != "status")
                             for c in self.fake.request.call_args_list))
        state = next(c.args[1] for c in self.fake.request.call_args_list if c.args[0] == "getState")
        self.assertEqual((state["maxElements"], state["maxDepth"]), (50, 6))
        self.error("TARGET_REQUIRED", "quietbridge_situation", appHandle="a")

    def test_situation_text_and_structured_ax_are_bounded_identically(self):
        original = self.request
        def request(method, params=None, **kwargs):
            result = original(method, params, **kwargs)
            if method == "getState":
                result["tree"] = "x" * (server.MAX_TREE_CHARS + 1)
            return result
        self.fake.request.side_effect = request
        result = self.rpc("quietbridge_situation", appHandle="a", windowId=7)
        situation = result["structuredContent"]["situation"]
        self.assertEqual(json.loads(self.text(result).split("\n", 1)[1]), situation)
        self.assertEqual(len(situation["ax"]["tree"]), server.MAX_TREE_CHARS)
        self.assertTrue(situation["ax"]["truncated"])

    def test_empty_ax_tree_uses_explicit_visibility_without_activating(self):
        original = self.request
        def request(method, params=None, **kwargs):
            result = original(method, params, **kwargs)
            if method == "getState":
                result["tree"] = ""
            return result
        self.fake.request.side_effect = request
        for visibility in (True, False, None):
            with self.subTest(visibility=visibility):
                self.windows = [{"windowId": 7, "pid": 88}]
                if visibility is not None:
                    self.windows[0]["isOnScreen"] = visibility
                self.fake.request.reset_mock()
                result = server.tool_state({"appHandle": "a", "windowId": 7})
                self.assertIn("(空)", result)
                self.assertEqual("isOnScreen=false" in result, visibility is False)
                self.assertFalse(any(c.args[0] in ("activate", "screenshot", "visualState", "preview")
                                     for c in self.fake.request.call_args_list))

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
            self.assertFalse(any(c.args[0] == "preview" and c.args[1].get('action') == 'hold' for c in self.fake.request.call_args_list))

    def test_observation_share_failure_and_read_only_stop_before_ax(self):
        self.bind()
        original = self.request
        def request(method, params=None, **kwargs):
            result = original(method, params, **kwargs)
            if method == "preview":
                result["preview"]["streamReady"] = False
            return result
        self.fake.request.side_effect = request
        for name in ("quietbridge_state", "quietbridge_visual_state", "quietbridge_screenshot"):
            self.fake.request.reset_mock()
            result = self.rpc(name, windowId=7)
            self.assertEqual(result["structuredContent"]["errorCode"], "PREVIEW_NOT_READY")
            self.assertFalse(any(c.args[0] in ("getState", "visualState", "screenshot") for c in self.fake.request.call_args_list))
        with mock.patch.object(server, "READ_ONLY", True):
            result = self.rpc("quietbridge_state", windowId=7)
            self.assertEqual(result["structuredContent"]["errorCode"], "SHARE_REQUIRED")

    def test_create_target_bound_refuses_before_share_or_handler(self):
        bound = self.bind()
        self.fake.request.reset_mock()
        with mock.patch.object(server, "_events_handler") as original:
            result = self.rpc(server._events_tool["name"], action="create_target", taskId=bound["taskId"])
            self.assertEqual(result['structuredContent']['errorCode'], 'TARGET_ALREADY_BOUND')
            original.assert_not_called()
            self.assertFalse(any(c.args[0] == 'preview' for c in self.fake.request.call_args_list))

    def test_listing_preserves_selection_even_when_another_window_is_first(self):
        self.state['window']['a'] = 8
        result = self.rpc('quietbridge_windows', action='list', appHandle='a')
        self.assertFalse(result.get('isError'), result)
        self.assertEqual(self.state['window']['a'], 8)

    def test_resolve_window_keeps_cached_target_or_stops_when_lost(self):
        self.state['window']['a'] = 8
        self.assertEqual(server.resolve_window('a'), 8)
        self.windows = [self.windows[0]]
        with self.assertRaises(server.HelperError) as caught:
            server.resolve_window('a')
        self.assertEqual(caught.exception.code, 'TARGET_LOST')
        self.assertEqual(self.state['window']['a'], 8)

    def test_resolve_window_requires_explicit_selection_for_multiple_windows(self):
        with self.assertRaises(server.HelperError) as caught:
            server.resolve_window('a')
        self.assertEqual(caught.exception.code, 'TARGET_REQUIRED')
        self.assertEqual(self.state['window'], {})
        self.windows = [self.windows[1]]
        self.assertEqual(server.resolve_window('a'), 8)

    def test_create_target_reports_missing_share_target_without_handler(self):
        awaiting = self.payload(action="begin", interactionMode="desktop")
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
        result = self.rpc("quietbridge_click", windowId=7, x=1, y=2)
        self.assertTrue(result.get("isError"), result)
        self.assertEqual(result["structuredContent"]["execution"]["status"], "unknown")
        self.assertEqual(sum(c.args[0] == "click" for c in self.fake.request.call_args_list), 1)
        self.assertFalse(result["structuredContent"]["retry"]["replaySideEffects"])

    def test_end_releases_task_guard_and_keeps_history(self):
        bound = self.bind()
        self.acknowledge(bound)
        self.error("TARGET_MISMATCH", "quietbridge_state", windowId=9)
        with self.assertRaises(Exception):
            self.payload(action="end", taskId=bound["taskId"])
        ended = self.payload(action="end", taskId=bound["taskId"], confirm=True)
        self.assertEqual(ended["state"], "ended")
        saved = json.loads(self.path.read_text())
        self.assertIsNone(saved["activeTaskId"])
        self.assertEqual(saved["tasks"][bound["taskId"]]["state"], "ended")
        self.error('TASK_NOT_BOUND', 'quietbridge_state', appHandle='a', windowId=9)
        # 新任务仍可正常开始并绑定，历史记录不丢失。
        again = self.payload(action='begin', interactionMode='desktop')
        self.acknowledge(again)
        again = self.payload(action='bind', taskId=again['taskId'], appHandle='a', windowId=7, confirm=True)
        self.assertEqual(again["state"], "bound")
        self.assertEqual(len(json.loads(self.path.read_text())["tasks"]), 2)

    def test_share_keeps_preview_for_the_task_and_unshare_releases_it(self):
        self.bind()
        shared = self.rpc("quietbridge_windows", action="share", appHandle="a", windowId=7, durationSeconds=900)
        self.assertFalse(shared.get("isError"), self.text(shared))
        self.assertIn("holding=True", self.text(shared))
        previews = [c.args for c in self.fake.request.call_args_list if c.args[0] == "preview"]
        self.assertEqual(previews[-1], ("preview", {"action": "hold", "appHandle": "a",
                                                   "windowId": 7, "durationSeconds": 900,
                                                   "source": "application"}))
        released = self.rpc("quietbridge_windows", action="unshare", appHandle="a")
        self.assertFalse(released.get("isError"), self.text(released))
        previews = [c.args for c in self.fake.request.call_args_list if c.args[0] == "preview"]
        self.assertEqual(previews[-1], ("preview", {"action": "release"}))

    def test_unshare_after_phase_end_releases_only_preview_and_preserves_host_protection(self):
        bound = self.bind()
        ended = self.payload(action='end', taskId=bound['taskId'], confirm=True)
        self.assertEqual(ended['previewCleanup'], {'status': 'released', 'holding': False,
                                                  'visible': False, 'streamReady': False})
        self.assertTrue(server.POWER_GUARD['active'])
        self.fake.request.reset_mock()
        result = self.rpc('quietbridge_windows', action='unshare')
        self.assertFalse(result.get('isError'), self.text(result))
        self.assertIn('holding=False', self.text(result))
        self.fake.request.assert_called_once()
        self.assertEqual(self.fake.request.call_args.args, ('preview', {'action': 'release'}))
        self.assertTrue(server.POWER_GUARD['active'])

    def test_phase_end_does_not_equate_panel_release_with_capture_stop_confirmation(self):
        for state in ('requested', 'failed', 'completed'):
            with self.subTest(stopState=state):
                original = self.request
                def request(method, params=None, **kwargs):
                    if method == 'preview' and params == {'action': 'release'}:
                        return {'preview': {'holding': False, 'visible': False, 'streamReady': False,
                                'captureStop': {'streamGeneration': 'old-stream', 'trackingWindowId': 7,
                                                'stopState': state, 'stopConfirmed': True,
                                                'message': 'private-content'}}}
                    return original(method, params, **kwargs)
                self.fake.request.side_effect = request
                task = self.payload(action="begin", interactionMode="desktop")
                self.acknowledge(task)
                bound = self.payload(action="bind", taskId=task["taskId"], appHandle="a", windowId=7, confirm=True)
                ended = self.payload(action='end', taskId=bound['taskId'], confirm=True)
                cleanup = ended['previewCleanup']
                self.assertEqual(cleanup['status'], 'released')
                self.assertEqual(cleanup['captureStop']['stopState'], state)
                self.assertEqual(cleanup['captureStop']['stopConfirmed'], state == 'completed')
                self.assertNotIn('private-content', json.dumps(cleanup))

    def test_unshare_with_closed_native_connection_never_restarts_helper(self):
        bound = self.bind()
        self.payload(action='end', taskId=bound['taskId'], confirm=True)
        self.fake.request.reset_mock()
        self.fake._alive.return_value = False
        result = self.rpc('quietbridge_windows', action='unshare')
        self.assertFalse(result.get('isError'), self.text(result))
        self.assertIn('nativeConnection=closed', self.text(result))
        self.fake.request.assert_not_called()

    def test_phase_end_reports_preview_release_failure_without_claiming_success(self):
        bound = self.bind()
        def fail_preview(method, params=None, **kwargs):
            if method == 'preview':
                raise server.HelperError('BRIDGE_TIMEOUT', 'synthetic release timeout')
            return self.request(method, params, **kwargs)
        self.fake.request.side_effect = fail_preview
        ended = self.payload(action='end', taskId=bound['taskId'], confirm=True)
        self.assertEqual(ended['state'], 'ended')
        self.assertEqual(ended['previewCleanup'], {'status': 'failed', 'errorCode': 'BRIDGE_TIMEOUT'})
        self.assertTrue(server.POWER_GUARD['active'])

    def test_replaced_snapshot_is_stale_without_changing_task_binding(self):
        bound = self.bind()
        self.state.update(snapshot='snap-new', snapshot_app='a', snapshot_window=7)
        result = self.rpc('quietbridge_click', appHandle='a', snapshotId='snap-old', elementIndex=1)
        self.assertEqual(result['structuredContent']['errorCode'], 'STALE_SNAPSHOT')
        self.assertEqual(self.payload(action='status')['taskId'], bound['taskId'])

    def test_share_can_cover_the_whole_desktop(self):
        self.bind()
        result = self.rpc("quietbridge_windows", action="share", appHandle="a", windowId=7, source="desktop")
        self.assertFalse(result.get("isError"), self.text(result))
        self.assertIn("整个桌面", self.text(result))
        sent = [c.args for c in self.fake.request.call_args_list if c.args[0] == "preview"][-1][1]
        self.assertEqual(sent["source"], "desktop")
        self.error("INVALID_ARGUMENT", "quietbridge_windows", action="share", appHandle="a", windowId=7, source="screen")

    def test_explicit_scope_survives_observation_and_input(self):
        self.bind()
        for source in ("desktop", "application", "window"):
            self.rpc("quietbridge_windows", action="share", appHandle="a", windowId=7, source=source)
            for name, args in (("quietbridge_state", {}), ("quietbridge_click", {"x": 1, "y": 2})):
                self.fake.request.reset_mock()
                result = self.rpc(name, windowId=7, **args)
                self.assertFalse(result.get("isError"), result)
                previews = [c.args[1] for c in self.fake.request.call_args_list if c.args[0] == "preview"]
                self.assertEqual(previews[-1]["source"], source)
                self.assertEqual(result["structuredContent"]["share"]["source"], source)

    def test_unshare_and_new_task_reset_to_application(self):
        first = self.bind()
        self.acknowledge(first)
        self.rpc("quietbridge_windows", action="share", windowId=7, source="desktop")
        self.rpc("quietbridge_windows", action="unshare")
        result = self.rpc("quietbridge_state", windowId=7)
        self.assertEqual(result["structuredContent"]["share"]["source"], "application")
        self.rpc("quietbridge_windows", action="share", windowId=7, source="desktop")
        self.payload(action='end', taskId=first['taskId'], confirm=True)
        fresh = self.payload(action='begin', interactionMode='desktop')
        self.acknowledge(fresh)
        self.payload(action='bind', taskId=fresh['taskId'], appHandle='a', windowId=7, confirm=True)
        result = self.rpc("quietbridge_state", windowId=7)
        self.assertEqual(result["structuredContent"]["share"]["source"], "application")

    def test_desktop_scope_is_not_inherited_by_other_targets_or_connections(self):
        server.ensure_share("a", 7, source="desktop")
        server.ensure_share("a", 8)
        self.assertEqual(server.SHARE_RECORD["source"], "application")
        server.ensure_share("a", 7, source="desktop")
        server.ensure_share("b", 7)
        self.assertEqual(server.SHARE_RECORD["source"], "application")
        server.ensure_share("a", 7, source="desktop")
        self.fake.connection_id = "connection-2"
        server.ensure_share("a", 7)
        self.assertEqual(server.SHARE_RECORD["source"], "application")

    def test_desktop_missing_target_never_downgrades_to_window_or_dispatches_input(self):
        self.bind()
        self.rpc("quietbridge_windows", action="share", windowId=7, source="desktop")
        original = self.request
        for membership in (False, None):
            def request(method, params=None, **kwargs):
                result = original(method, params, **kwargs)
                if method == "preview":
                    result["preview"]["targetOnCurrentSpace"] = membership
                return result
            self.fake.request.side_effect = request
            self.fake.request.reset_mock()
            self.error("PREVIEW_NOT_READY", "quietbridge_click", windowId=7, x=1, y=2)
            self.assertEqual(server.SHARE_SELECTION["source"], "desktop")
            calls = self.fake.request.call_args_list
            self.assertFalse(any(c.args[0] in server.INPUT_METHODS for c in calls))
            self.assertFalse(any(c.args[0] == "preview" and c.args[1].get("source") == "window" for c in calls))

    def test_failed_scope_change_does_not_reuse_previous_desktop_selection(self):
        server.ensure_share("a", 7, source="desktop")
        with mock.patch.object(server, "request_share", side_effect=server.HelperError("PREVIEW_NOT_READY", "fixture")):
            with self.assertRaises(server.HelperError):
                server.ensure_share("a", 7, source="application")
        self.assertEqual(server.SHARE_SELECTION["source"], "application")

    def test_share_schema_describes_surface_default_and_desktop_for_cross_app(self):
        tool = server.TOOL_MAP["quietbridge_windows"]
        source = tool["inputSchema"]["properties"]["source"]
        self.assertNotIn('default', source)
        self.assertIn('连续视频', tool['description'])
        self.assertIn('绘制脉冲', tool['description'])
        self.assertEqual(set(source["enum"]), {"application", "desktop", "window"})
        self.assertIn("跨应用", tool["description"])

    def test_share_never_retargets_the_bound_window_or_accepts_a_bad_lease(self):
        self.bind()
        self.error("TARGET_MISMATCH", "quietbridge_windows", action="share", appHandle="a", windowId=8)
        for duration in (5, 4000, "600", True):
            self.error("INVALID_ARGUMENT", "quietbridge_windows", action="share", appHandle="a",
                       windowId=7, durationSeconds=duration)
        self.error("INVALID_ARGUMENT", "quietbridge_windows", action="share", appHandle="a")
        self.assertFalse(any(c.args[0] == "preview" for c in self.fake.request.call_args_list))

    def test_share_requires_a_bound_task(self):
        self.payload(action="begin", interactionMode="desktop")
        self.error("TASK_NOT_BOUND", "quietbridge_windows", action="share", appHandle="a", windowId=7)

    def test_end_releases_the_share_lease_after_the_task(self):
        bound = self.bind()
        self.rpc("quietbridge_windows", action="share", appHandle="a", windowId=7)
        self.fake.request.reset_mock()
        ended = self.payload(action="end", taskId=bound["taskId"], confirm=True)
        self.assertEqual(ended["state"], "ended")
        self.assertEqual([c.args for c in self.fake.request.call_args_list if c.args[0] == "preview"],
                         [("preview", {"action": "release"})])

    def test_background_share_reuses_native_window_capture_across_observation_and_clicks(self):
        self.bind()
        original = self.request
        on_current_space = False

        def native_capture(method, params=None, **kwargs):
            result = original(method, params, **kwargs)
            if method == 'preview':
                result['preview'].update(
                    targetOnCurrentSpace=on_current_space,
                    captureMode='application' if on_current_space else 'window',
                    scope='application_current_space' if on_current_space else 'window')
            return result

        self.fake.request.side_effect = native_capture
        for name, args in [('quietbridge_state', {}), ('quietbridge_click', {'x': 1, 'y': 2})] * 3:
            self.fake.request.reset_mock()
            result = self.rpc(name, windowId=7, **args)
            self.assertFalse(result.get('isError'), result)
            previews = [c.args[1] for c in self.fake.request.call_args_list if c.args[0] == 'preview']
            self.assertEqual([p['source'] for p in previews], ['application'],
                             'Native capture is already window-scoped; a second hold restarts the stream')
            share = result['structuredContent']['share']
            self.assertEqual(share['source'], 'window')
            self.assertEqual(share['scope'], 'window')
            self.assertEqual(share['requestedSource'], 'application')
            self.assertIn('background_space', share['reason'])
            self.assertEqual(server.SHARE_SELECTION['source'], 'application')
            self.assertFalse(any(c.args[0] in ('activate', 'show') for c in self.fake.request.call_args_list))

        # Returning to the current Space must still allow the requested application scope.
        on_current_space = True
        self.fake.request.reset_mock()
        result = self.rpc('quietbridge_state', windowId=7)
        self.assertFalse(result.get('isError'), result)
        self.assertEqual(result['structuredContent']['share']['source'], 'application')
        self.assertEqual(result['structuredContent']['share']['scope'], 'application_current_space')
        previews = [c.args[1] for c in self.fake.request.call_args_list if c.args[0] == 'preview']
        self.assertEqual([p['source'] for p in previews], ['application'])

    def test_unknown_space_reports_native_window_scope_without_restarting_capture(self):
        original = self.request

        def native_capture(method, params=None, **kwargs):
            result = original(method, params, **kwargs)
            if method == 'preview':
                result['preview'].update(targetOnCurrentSpace=None, captureMode='window', scope='window')
            return result

        self.fake.request.side_effect = native_capture
        server.ensure_share('a', 7)
        previews = [c.args[1] for c in self.fake.request.call_args_list if c.args[0] == 'preview']
        self.assertEqual([p['source'] for p in previews], ['application'])
        self.assertEqual(server.SHARE_RECORD['source'], 'window')
        self.assertEqual(server.SHARE_RECORD['requestedSource'], 'application')
        self.assertIsNone(server.SHARE_RECORD['targetOnCurrentSpace'])
        self.assertNotIn('background_space', server.SHARE_RECORD['reason'])

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
                    __enter__=lambda s: {'tasks': {'t': {'windowId': None, 'interactionMode': 'desktop'}}, 'activeTaskId': 't'},
                    __exit__=lambda *args: None))):
            self.assertEqual(server._events_dispatch({'action': 'create_target', 'taskId': 't'}), '{"status":"queued"}')
            shared.assert_called_once_with('a', {1, 2})

    def test_unbound_create_reports_share_pending_without_claiming_success(self):
        with mock.patch.object(server, 'browser_window_snapshot', return_value=('a', set())), \
                mock.patch.object(server, '_events_handler', return_value='{"status":"queued"}'), \
                mock.patch.object(server, 'share_new_window', side_effect=server.HelperError('PREVIEW_NOT_READY', 'no window')), \
                mock.patch.object(server, 'task_store', mock.MagicMock(return_value=mock.MagicMock(
                    __enter__=lambda s: {'tasks': {'t': {'windowId': None, 'interactionMode': 'desktop'}}, 'activeTaskId': 't'},
                    __exit__=lambda *args: None))):
            output = server._events_dispatch({'action': 'create_target', 'taskId': 't'})
            self.assertIn('共享待补', output)
            self.assertIn('PREVIEW_NOT_READY', output)

    def test_background_create_waits_for_mapping_without_guessing_native_window(self):
        task = self.payload(action='begin')
        with mock.patch.object(server, 'browser_window_snapshot') as windows, \
                mock.patch.object(server, 'share_new_window') as share, \
                mock.patch.object(server, '_events_handler', return_value='{"status":"queued"}'):
            output = json.loads(server._events_dispatch({'action': 'create_target', 'taskId': task['taskId']}))
            self.assertEqual(output['status'], 'queued')
            self.assertEqual(output['sharePending'], 'browser_mapping_required')
            windows.assert_not_called()
            share.assert_not_called()

    def test_lost_persistent_record_never_disables_guard(self):
        self.bind()
        self.path.unlink()
        self.fake.request.reset_mock()
        self.error("TASK_STORE_ERROR", "quietbridge_state", windowId=8)
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
        cases = (("quietbridge_click", {"x": 1, "y": 2}), ("quietbridge_hover", {"x": 1, "y": 2}),
                 ("quietbridge_scroll", {"x": 1, "y": 2}),
                 ("quietbridge_drag", {"fromX": 1, "fromY": 2, "toX": 3, "toY": 4}),
                 ("quietbridge_visual_state", {}), ("quietbridge_wait_ready", {"expectedText": "x"}))
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
            reply = self.rpc("quietbridge_click", windowId=7, x=1, y=2)
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
        now = [server.time.monotonic()]
        original = self.fake.request.side_effect
        waits = []
        def read(method, params=None, **kwargs):
            if method == "windowIdentity":
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
                mock.patch.object(server.REQUEST_CANCELLED, "wait", side_effect=wait):
            result = self.rpc("quietbridge_wait_ready", windowId=7, timeoutSeconds=5, intervalSeconds=1)
        self.assertFalse(result.get("isError"), result)
        self.assertTrue(json.loads(self.text(result).split("\n")[0])["ready"])
        self.assertEqual([c.kwargs["timeout"] for c in self.fake.request.call_args_list], [5, 4, 4, 3])
        shares = [c for c in self.fake.request.call_args_list if c.args[0] == "preview"]
        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0].args[1]["source"], "application")
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
        now = [server.time.monotonic()]
        def wait(seconds):
            now[0] += seconds
            self.fake.connection_id = "changed-during-wait"
        with mock.patch.object(server.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(server.REQUEST_CANCELLED, "wait", side_effect=wait):
            self.error("REQUIRES_REVALIDATION", "quietbridge_wait_ready", windowId=7, timeoutSeconds=5, intervalSeconds=1)
        self.assertEqual(sum(c.args[0] == "getState" for c in self.fake.request.call_args_list), 1)

    def test_bind_confirmation_readonly_and_incomplete_native_response(self):
        self.payload(action="begin", interactionMode="desktop")
        self.error("INVALID_ARGUMENT", action="bind", appHandle="a", windowId=7, confirm=1)
        with mock.patch.object(server, "READ_ONLY", True):
            self.error("READ_ONLY", action="bind", appHandle="a", windowId=7, confirm=True)
        self.fake.request.assert_not_called()
        self.fake.request.side_effect = None
        self.fake.request.return_value = {"state": "attached"}
        self.error("ATTACH_UNVERIFIED", action="bind", appHandle="a", windowId=7, confirm=True)
        self.assertEqual(json.loads(self.path.read_text())["tasks"][json.loads(self.path.read_text())["activeTaskId"]]["state"], "awaiting_window")

    def test_bind_holds_session_protection_and_phase_end_retains_it(self):
        # 锁屏不会终止进程，空闲睡眠才会；断言必须跟着任务租约出现和消失。
        # 隔离正在运行的菜单栏，让这里只验证无面板时的既有参数。
        with mock.patch.object(server, "control_snapshot", return_value={"master": True}):
            bound = self.bind()
        self.assertTrue(bound["powerGuard"]["holding"])
        self.assertTrue(bound["powerGuard"]["privacy"]["active"])
        self.assertTrue(bound["powerGuard"]["privacy"]["shieldVisible"])
        holds = [c.args for c in self.fake.request.call_args_list if c.args[0] == "powerGuard"]
        self.assertEqual(holds, [("powerGuard", {"action": "hold",
                                                 "durationSeconds": server.POWER_GUARD_SECONDS})])
        self.assertTrue(server.POWER_GUARD["active"])

        ended = self.payload(action="end", confirm=True)
        self.assertTrue(ended['sessionProtection']['retained'])
        self.assertTrue(server.POWER_GUARD["active"])
        actions = [c.args[1]["action"] for c in self.fake.request.call_args_list if c.args[0] == "powerGuard"]
        self.assertEqual(actions, ["hold"])

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
