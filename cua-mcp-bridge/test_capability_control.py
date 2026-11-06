import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("CUA_CONFIG_DIR", tempfile.mkdtemp(prefix="cua-test-config-"))
import server


class CapabilityControlTests(unittest.TestCase):
    def test_missing_menu_bar_keeps_existing_behavior(self):
        # Isolate from a menu bar that may actually be running on this machine.
        with mock.patch.object(server.urllib.request, "urlopen",
                               side_effect=OSError("connection refused")):
            self.assertIsNone(server.control_snapshot())
        self.assertIsNone(server.capability_block("cua_click", {}, {}))

    def test_switch_blocks_only_its_capability(self):
        switches = {"master": True, "native_input": False, "native_observe": True}
        self.assertEqual(server.capability_block("cua_click", {}, switches), "native_input")
        self.assertIsNone(server.capability_block("cua_state", {}, switches))
        self.assertEqual(server.capability_block("cua_windows", {"action": "activate"}, switches), "native_input")
        self.assertIsNone(server.capability_block("cua_windows", {"action": "list"}, switches))

    def test_master_leaves_permission_status_available(self):
        switches = {"master": False}
        self.assertEqual(server.capability_block("cua_click", {}, switches), "master")
        self.assertIsNone(server.capability_block("cua_permissions", {"action": "status"}, switches))

    def test_failure_reads_the_switch_again(self):
        snapshots = iter(({"master": True, "native_input": True}, {"master": True, "native_input": False}))
        with mock.patch.object(server, "control_snapshot", side_effect=lambda: next(snapshots)), \
                mock.patch.object(server, "task_guard", side_effect=server.HelperError("TARGET_LOST", "窗口不存在")):
            result = server._call_tool("cua_click", {"appHandle": "a", "windowId": 7, "x": 1, "y": 1})
        self.assertEqual(result["errorCode"], "CAPABILITY_DISABLED")

    def test_failure_keeps_original_error_when_capability_remains_enabled(self):
        with mock.patch.object(server, "control_snapshot", return_value={"master": True, "native_input": True}), \
                mock.patch.object(server, "task_guard", side_effect=server.HelperError("TARGET_LOST", "窗口不存在")):
            result = server._call_tool("cua_click", {"appHandle": "a", "windowId": 7, "x": 1, "y": 1})
        self.assertEqual(result["errorCode"], "TARGET_LOST")

    def test_new_power_lease_records_locked_use_but_renewal_does_not(self):
        server.POWER_GUARD.update(active=False, nextRenewAt=0)
        with mock.patch.object(server, "control_snapshot", return_value={"locked_use": False}), \
                mock.patch.object(server, "call_helper", return_value={"guard": {"holding": True}}) as helper:
            server.hold_power_guard()
            self.assertIs(helper.call_args.args[1]["lockedUse"], False)
            server.POWER_GUARD.update(active=True, nextRenewAt=0)
            server.hold_power_guard()
            self.assertNotIn("lockedUse", helper.call_args.args[1])
        server.POWER_GUARD.update(active=False, nextRenewAt=0)


if __name__ == "__main__":
    unittest.main()
