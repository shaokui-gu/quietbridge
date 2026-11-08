import importlib.util
import hashlib
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("auth_installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class RenameUpgradeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('auth_upgrade', ROOT / 'upgrade_plugin.py')
        self.upgrade = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'install': installer}):
            spec.loader.exec_module(self.upgrade)

    def test_installed_legacy_and_new_executables_can_both_be_compared(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / 'plugin.bundle'
            (bundle / 'Contents/MacOS').mkdir(parents=True)
            for name in ('WorkBuddyCUAAuthorizationPlugin', 'QuietBridgeAuthorizationPlugin'):
                (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': name}))
                (bundle / 'Contents/MacOS' / name).write_bytes(b'fixture')
                self.assertEqual(self.upgrade.binary_digest(bundle), hashlib.sha256(b'fixture').hexdigest())

    def test_executable_name_cannot_escape_the_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            (bundle / 'Contents').mkdir()
            (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': '../../outside'}))
            with self.assertRaisesRegex(RuntimeError, '无法识别'):
                self.upgrade.binary_digest(bundle)


class InstallationGateTests(unittest.TestCase):
    def test_deny_only_rejects_normal_bundle_before_policy_access(self):
        with patch.object(installer, "require_root"), \
             patch.object(installer, "verify_bundle", return_value=""), \
             patch.object(Path, "read_bytes", return_value=plistlib.dumps({})), \
             patch.object(installer, "authorization_read") as read:
            with self.assertRaisesRegex(RuntimeError, "CUADenyOnly"):
                installer.install(experimental=True, deny_only=True)
            read.assert_not_called()

    def test_install_stops_before_any_system_access(self):
        with patch.object(installer, "DIRECT_UI_VALIDATED_AFTER_FRESH_LOGIN", False), \
             patch.object(installer, "require_root") as root, \
             patch.object(installer, "authorization_read") as read, \
             patch.object(installer, "authorization_write") as write:
            for experimental in (False, True):
                for deny_only in (False, True):
                    if experimental and deny_only:
                        continue
                    with self.subTest(experimental=experimental, deny_only=deny_only):
                        with self.assertRaisesRegex(RuntimeError, "安装已禁用"):
                            installer.install(experimental=experimental, deny_only=deny_only)
            root.assert_not_called()
            read.assert_not_called()
            write.assert_not_called()

    def test_prepare_stops_before_staging_publishing_or_sudo(self):
        spec = importlib.util.spec_from_file_location("auth_prepare", ROOT / "prepare_install.py")
        prepare = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"install": installer}):
            spec.loader.exec_module(prepare)
        with patch.object(installer, "DIRECT_UI_VALIDATED_AFTER_FRESH_LOGIN", False), \
             patch.object(prepare.tempfile, "TemporaryDirectory") as stage, \
             patch.object(prepare.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "注销重新登录"):
                prepare.main()
            stage.assert_not_called()
            run.assert_not_called()

    def test_auto_unlock_gate_opens_after_fresh_login_validation(self):
        self.assertTrue(installer.DIRECT_UI_VALIDATED_AFTER_FRESH_LOGIN)
        installer.require_supported_lock_ui()


class CandidatePolicyTests(unittest.TestCase):
    def test_keeps_native_ui_marker_directly_in_primary(self):
        original = {"class": "rule", "rule": ["use-login-window-ui"],
                    "tries": 10000, "comment": "original system policy"}
        plan = installer.candidate_policy(plistlib.dumps(original))
        self.assertNotIn(installer.STOCK_RIGHT, plan)
        self.assertEqual(plan[installer.RIGHT]["k-of-n"], 1)
        self.assertEqual(plan[installer.RIGHT]["rule"],
                         [installer.REMOTE_RIGHT, "use-login-window-ui"])
        self.assertEqual(original["rule"], ["use-login-window-ui"])
        self.assertEqual(plan[installer.RIGHT]["comment"], original["comment"])
        self.assertNotIn("mechanisms", plan[installer.RIGHT])
        self.assertNotIn("shared", plan[installer.RIGHT])
        self.assertNotIn("tries", plan[installer.RIGHT])
        self.assertEqual(plan[installer.REMOTE_RIGHT]["tries"], 1)
        self.assertEqual(plan[installer.REMOTE_RIGHT]["mechanisms"],
                         [installer.PLUGIN_MECHANISM])

    def test_refuses_already_damaged_policy(self):
        source = {"class": "evaluate-mechanisms", "mechanisms": [installer.PLUGIN_MECHANISM]}
        with self.assertRaises(ValueError):
            installer.candidate_policy(plistlib.dumps(source))


class InstallationTransactionTests(unittest.TestCase):
    def test_validation_install_and_uninstall_restore_exact_original(self):
        self.exercise_transaction(fail_primary=False)

    def test_primary_write_failure_restores_original_and_keeps_backup(self):
        self.exercise_transaction(fail_primary=True)

    def exercise_transaction(self, fail_primary):
        original = {"class": "rule", "rule": ["use-login-window-ui"],
                    "version": 1, "comment": "original"}
        original_raw = plistlib.dumps(original)
        database = {installer.RIGHT: original}
        writes = []
        failed = False

        def write(right, value):
            nonlocal failed
            writes.append(right)
            database[right] = value
            if fail_primary and right == installer.RIGHT and not failed:
                failed = True
                raise RuntimeError("simulated read-back failure")
            return value

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = root / "source.bundle"
            (bundle / "Contents").mkdir(parents=True)
            (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({"CUADenyOnly": True}))
            state = root / "state"
            state_file = state / "installation.plist"
            original_file = state / "system.login.screensaver.original.plist"
            with patch.multiple(installer, STATE_DIRECTORY=state, STATE_FILE=state_file,
                                ORIGINAL_RIGHT_FILE=original_file,
                                INSTALLED_BUNDLE=root / "plugins" / installer.BUNDLE_NAME), \
                 patch.object(installer, "require_root"), \
                 patch.object(installer, "verify_bundle", return_value=""), \
                 patch.object(installer.subprocess, "run"), \
                 patch.object(installer, "authorization_read",
                              side_effect=lambda right=installer.RIGHT: plistlib.dumps(database[right])), \
                 patch.object(installer, "right_exists", side_effect=lambda right: right in database), \
                 patch.object(installer, "write_verified", side_effect=write), \
                 patch.object(installer, "remove_branch", side_effect=lambda right: database.pop(right, None)):
                if fail_primary:
                    with self.assertRaisesRegex(RuntimeError, "simulated"):
                        installer.install(bundle, experimental=True, deny_only=True)
                    self.assertEqual(database[installer.RIGHT], original)
                    self.assertEqual(plistlib.loads(state_file.read_bytes())["phase"], "entry-restored")
                else:
                    result = installer.install(bundle, experimental=True, deny_only=True)
                    self.assertEqual(writes, [installer.REMOTE_RIGHT, installer.RIGHT])
                    self.assertEqual(database[installer.RIGHT]["rule"],
                                     [installer.REMOTE_RIGHT, "use-login-window-ui"])
                    self.assertFalse(result["state"]["enabled"])
                    self.assertTrue(result["state"]["validatedAfterFreshLogin"])
                self.assertEqual(original_file.read_bytes(), original_raw)
                self.assertNotIn(installer.STOCK_RIGHT, database)
                installer.uninstall()
                self.assertEqual(database, {installer.RIGHT: original})
                self.assertFalse(state.exists())


class ReadbackVerificationTests(unittest.TestCase):
    def test_rejects_wrapping_native_ui_in_stock_branch(self):
        expected = {"class": "rule", "k-of-n": 1,
                    "rule": [installer.REMOTE_RIGHT, "use-login-window-ui"]}
        damaged = dict(expected, rule=[installer.REMOTE_RIGHT, installer.STOCK_RIGHT])
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


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.bundle = self.root / 'installed.bundle'
        (self.bundle / 'Contents').mkdir(parents=True)
        (self.bundle / 'Contents/version').write_text('old')
        self.source = self.root / 'source.bundle'
        (self.source / 'Contents').mkdir(parents=True)
        (self.source / 'Contents/version').write_text('new')
        (self.source / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CUADenyOnly': False, 'CFBundleShortVersionString': '0.4.0', 'CFBundleVersion': '8'}))
        self.original = self.state / 'original.plist'
        self.original.write_bytes(plistlib.dumps({'class': 'rule', 'rule': ['use-login-window-ui']}))
        self.state_file = self.state / 'installation.plist'
        self.database = installer.candidate_policy(self.original.read_bytes())
        self.metadata = {'version': 2, 'phase': 'committed', 'enabled': True,
                         'denyOnly': False, 'protocolVersion': 3, 'experimentalOptIn': True,
                         'policyTopology': 'direct-login-window-ui',
                         'bundleIdentifier': installer.PLUGIN_IDENTIFIER,
                         'teamIdentifier': installer.TEAM_IDENTIFIER, 'actualPolicies': self.database}
        installer.atomic_plist(self.state_file, self.metadata)
        mocks = [patch.multiple(installer, STATE_DIRECTORY=self.state, STATE_FILE=self.state_file,
                                ORIGINAL_RIGHT_FILE=self.original, INSTALLED_BUNDLE=self.bundle),
                 patch.object(installer, 'require_root'), patch.object(installer, 'require_root_owned'),
                 patch.object(installer, 'verify_bundle', return_value=''),
                 patch.object(installer.subprocess, 'run'),
                 patch.object(installer, 'authorization_read', side_effect=lambda right=installer.RIGHT: plistlib.dumps(self.database[right])),
                 patch.object(installer, 'right_exists', side_effect=lambda right: right in self.database)]
        for mock in mocks:
            mock.start()
            self.addCleanup(mock.stop)

    def test_upgrade_preserves_original_policy_backup(self):
        original = self.original.read_bytes()
        with patch.object(installer, 'authorization_write') as write:
            installer.upgrade(self.source)
        write.assert_not_called()
        self.assertEqual(self.original.read_bytes(), original)
        self.assertEqual((self.bundle / 'Contents/version').read_text(), 'new')
        metadata = plistlib.loads(self.state_file.read_bytes())
        self.assertEqual(metadata['phase'], 'committed')
        self.assertTrue(metadata['enabled'])
        self.assertEqual(metadata['actualPolicies'], self.metadata['actualPolicies'])

    def test_failed_upgrade_restores_binary_and_receipt(self):
        original, receipt = self.original.read_bytes(), self.state_file.read_bytes()
        with patch.object(installer, 'verify_bundle', side_effect=['', '', RuntimeError('signature failure')]):
            with self.assertRaisesRegex(RuntimeError, 'signature failure'):
                installer.upgrade(self.source)
        self.assertEqual((self.bundle / 'Contents/version').read_text(), 'old')
        self.assertEqual(self.state_file.read_bytes(), receipt)
        self.assertEqual(self.original.read_bytes(), original)

    def test_generic_identity_upgrade_keeps_old_policy_and_original_backup(self):
        self.metadata['bundleIdentifier'] = installer.LEGACY_PLUGIN_IDENTIFIER
        installer.atomic_plist(self.state_file, self.metadata)
        original = self.original.read_bytes()
        with patch.object(installer, 'authorization_write') as write:
            installer.upgrade(self.source)
        write.assert_not_called()
        updated = plistlib.loads(self.state_file.read_bytes())
        self.assertEqual(updated['bundleIdentifier'], installer.PLUGIN_IDENTIFIER)
        self.assertEqual(updated['actualPolicies'], self.metadata['actualPolicies'])
        self.assertEqual(self.original.read_bytes(), original)

    def test_identity_migration_rejects_a_foreign_installation_ledger(self):
        self.metadata['bundleIdentifier'] = 'com.other.AuthorizationPlugin'
        installer.atomic_plist(self.state_file, self.metadata)
        with patch.object(installer, 'authorization_write') as write:
            with self.assertRaisesRegex(RuntimeError, '需要恢复'):
                installer.upgrade(self.source)
        write.assert_not_called()
        self.assertEqual((self.bundle / 'Contents/version').read_text(), 'old')

    def test_foreign_policy_and_incomplete_install_cannot_be_overwritten(self):
        self.database[installer.RIGHT] = {'class': 'rule', 'rule': ['foreign']}
        with self.assertRaisesRegex(RuntimeError, '其他修改'):
            installer.upgrade(self.source)
        self.metadata['phase'] = 'rollback-needed'
        installer.atomic_plist(self.state_file, self.metadata)
        with self.assertRaisesRegex(RuntimeError, '需要恢复'):
            installer.upgrade(self.source)
        self.assertEqual((self.bundle / 'Contents/version').read_text(), 'old')

    def test_enable_requires_a_valid_grant_mode_installation(self):
        self.metadata['enabled'] = False
        installer.atomic_plist(self.state_file, self.metadata)
        installer.enable()
        self.assertTrue(plistlib.loads(self.state_file.read_bytes())['enabled'])
        self.metadata['denyOnly'] = True
        installer.atomic_plist(self.state_file, self.metadata)
        with self.assertRaisesRegex(RuntimeError, '不支持启用'):
            installer.enable()


if __name__ == "__main__":
    unittest.main()
