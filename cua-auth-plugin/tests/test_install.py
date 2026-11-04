import importlib.util
from pathlib import Path
import plistlib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("auth_installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallationGateTests(unittest.TestCase):
    def test_install_stops_before_any_system_access(self):
        with patch.object(installer, "require_root") as root, \
             patch.object(installer, "authorization_read") as read, \
             patch.object(installer, "authorization_write") as write:
            with self.assertRaisesRegex(RuntimeError, "安装已禁用"):
                installer.install()
            root.assert_not_called()
            read.assert_not_called()
            write.assert_not_called()


class CandidatePolicyTests(unittest.TestCase):
    def test_keeps_original_as_separate_branch(self):
        original = {"class": "rule", "rule": ["use-login-window-ui"], "tries": 10000}
        plan = installer.candidate_policy(plistlib.dumps(original))
        self.assertEqual(plan[installer.STOCK_RIGHT], original)
        self.assertEqual(plan[installer.RIGHT]["k-of-n"], 1)
        self.assertEqual(plan[installer.RIGHT]["rule"],
                         [installer.REMOTE_RIGHT, installer.STOCK_RIGHT])
        self.assertNotIn("mechanisms", plan[installer.RIGHT])
        self.assertNotIn("shared", plan[installer.RIGHT])
        self.assertNotIn("tries", plan[installer.RIGHT])
        self.assertEqual(plan[installer.REMOTE_RIGHT]["mechanisms"],
                         [installer.PLUGIN_MECHANISM])

    def test_refuses_already_damaged_policy(self):
        source = {"class": "evaluate-mechanisms", "mechanisms": [installer.PLUGIN_MECHANISM]}
        with self.assertRaises(ValueError):
            installer.candidate_policy(plistlib.dumps(source))


class ReadbackVerificationTests(unittest.TestCase):
    def test_rejects_lost_stock_branch(self):
        expected = {"class": "rule", "k-of-n": 1,
                    "rule": [installer.REMOTE_RIGHT, installer.STOCK_RIGHT]}
        damaged = {"class": "evaluate-mechanisms", "mechanisms": [installer.PLUGIN_MECHANISM]}
        self.assertFalse(installer.policy_matches(damaged, expected))

    def test_rejects_unexpected_policy_flags(self):
        expected = {"class": "rule", "rule": ["use-login-window-ui"]}
        actual = dict(expected, **{"authenticate-user": False})
        self.assertFalse(installer.policy_matches(actual, expected))

    def test_ignores_database_timestamps_only(self):
        expected = {"class": "rule", "rule": ["use-login-window-ui"]}
        self.assertTrue(installer.policy_matches(dict(expected, created=1, modified=2), expected))

    def test_accepts_only_exact_apple_creator_pair(self):
        expected = {"class": "rule", "rule": ["use-login-window-ui"]}
        actual = dict(expected, identifier="com.apple.security",
                      requirement='identifier "com.apple.security" and anchor apple')
        self.assertTrue(installer.policy_matches(actual, expected))
        actual["requirement"] = "allow"
        self.assertFalse(installer.policy_matches(actual, expected))
        actual.pop("requirement")
        self.assertFalse(installer.policy_matches(actual, expected))


if __name__ == "__main__":
    unittest.main()
