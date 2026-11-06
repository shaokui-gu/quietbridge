import importlib.util
import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('package_deploy', Path(__file__).with_name('deploy.py'))
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)
ROOT = Path(__file__).resolve().parents[2]


class FakeService:
    def __init__(self, fail=False):
        self.starts = 0
        self.stops = 0
        self.fail = fail

    def stop(self):
        self.stops += 1

    def start(self):
        self.starts += 1

    def wait_ready(self, executable, info):
        if self.fail:
            raise RuntimeError('simulated startup failure')
        if info['CFBundleVersion'] != '55' or not executable.is_file():
            raise RuntimeError('wrong deployed version')


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.runtime = self.home / 'Library/Application Support/workbuddy-cua'
        self.config = self.home / '.workbuddy/mcp.json'
        self.agent = self.home / 'Library/LaunchAgents' / (deploy.LABEL + '.plist')
        self.extension = self.runtime / 'extension/extension-stage'
        self.app = self.root / 'menu.app'
        (self.app / 'Contents/MacOS').mkdir(parents=True)
        (self.app / 'Contents/MacOS/workbuddy-cua-native').write_bytes(b'new-menu')
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '0.5.27', 'CFBundleVersion': '55'}))
        bridge = self.app / 'Contents/Resources/bridge'
        bridge.mkdir(parents=True)
        (bridge / 'server.py').write_text('import json; print(json.dumps({"result":{"serverInfo":{"name":"workbuddy-cua"}}}))')
        self.support = self.root / 'support'
        extension = self.support / 'extension/extension-stage'
        extension.mkdir(parents=True)
        for name in ('manifest.json', 'native_host.py', 'install_native_host.py'):
            (extension / name).write_bytes((ROOT / 'browser-observer-extension' / name).read_bytes())
        (extension / 'browser_events.py').write_bytes((ROOT / 'cua-mcp-bridge/browser_events.py').read_bytes())
        (extension / 'popup.html').write_text('new-ui')
        helper_app = self.root / 'helper.app'
        (helper_app / 'Contents/MacOS').mkdir(parents=True)
        (helper_app / 'Contents/Info.plist').write_bytes((self.app / 'Contents/Info.plist').read_bytes())
        self.helper = helper_app / 'Contents/MacOS/workbuddy-cua-native'
        self.helper.write_bytes(b'independent-helper')
        self.original = {'mcpServers': {'other': {'command': 'untouched'}, 'workbuddy-cua': {
            'command': 'old-python', 'args': ['-B', str(deploy.APP / 'Contents/Resources/bridge/server.py')],
            'env': {'CUA_HELPER_PATH': 'old-public-helper', 'KEEP_ME': 'value'}, 'custom': True}}}
        deploy.atomic(self.config, json.dumps(self.original).encode())
        deploy.atomic(self.agent, plistlib.dumps({'Label': deploy.LABEL, 'ProgramArguments': [str(deploy.APP / 'Contents/MacOS/workbuddy-cua-native'), '--menubar']}), 0o644)
        self.extension.mkdir(parents=True)
        (self.extension / 'old-ui').write_text('preserve')
        # Tests exercise actual staging, handshake and user-file transactions;
        # only signing/system processes are replaced.
        for name in ('verify_helper', 'verify_code'):
            mock = patch.object(deploy, name)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(deploy.platform, 'mac_ver', return_value=('14.0', ('', '', ''), ''))
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(deploy.platform, 'machine', return_value='arm64')
        mock.start()
        self.addCleanup(mock.stop)

    def install(self, service):
        return deploy.deploy(self.home, self.helper, self.app, self.support, service)

    def test_success_preserves_settings_and_uses_independent_helper(self):
        service = FakeService()
        outcome = self.install(service)
        data = json.loads(self.config.read_bytes())
        self.assertEqual(data['mcpServers']['other'], self.original['mcpServers']['other'])
        entry = data['mcpServers']['workbuddy-cua']
        self.assertEqual(entry['env'], {'CUA_HELPER_PATH': str(self.helper), 'KEEP_ME': 'value'})
        self.assertTrue(entry['custom'])
        self.assertTrue(Path(entry['args'][-1]).is_file())
        self.assertFalse(outcome['startupVerified'])
        self.assertEqual(service.starts, 0)
        self.assertEqual(service.stops, 1)
        self.assertFalse(plistlib.loads(self.agent.read_bytes())['RunAtLoad'])
        self.assertEqual(json.loads(Path(outcome['journal']).read_bytes())['phase'], 'committed')
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.extension / 'popup.html').is_file())
        again = self.install(FakeService())
        self.assertEqual(again['helper'], outcome['helper'])
        self.assertEqual(len(list((self.runtime / 'releases').glob('[!.]*'))), 1)

    def test_commit_failure_restores_all_user_components(self):
        original, agent = self.config.read_bytes(), self.agent.read_bytes()
        service = FakeService()
        apply = deploy.Transaction.apply
        def fail_after_apply(transaction):
            apply(transaction)
            raise RuntimeError('simulated commit failure')
        with patch.object(deploy.Transaction, 'apply', fail_after_apply):
            with self.assertRaisesRegex(RuntimeError, 'simulated commit failure'):
                self.install(service)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.agent.read_bytes(), agent)
        self.assertEqual((self.extension / 'old-ui').read_text(), 'preserve')
        self.assertFalse((self.extension / 'popup.html').exists())
        self.assertFalse((self.home / 'Library/Application Support/WorkbuddyExtension/browser-observer-extension/native_host.py').exists())
        self.assertEqual(service.starts, 1)  # Restore only the previous agent.
        journal = next((self.runtime / 'transactions').glob('*/journal.json'))
        self.assertEqual(json.loads(journal.read_bytes())['phase'], 'rolled-back')
        backups = list(journal.parent.glob('backup-*'))
        self.assertTrue(backups)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in backups))

    def test_handshake_failure_never_touches_user_files(self):
        bridge = self.app / 'Contents/Resources/bridge/server.py'
        bridge.write_text('raise SystemExit(1)')
        original = self.config.read_bytes()
        service = FakeService()
        with self.assertRaisesRegex(ValueError, '握手失败'):
            self.install(service)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(service.stops, 0)
        self.assertTrue((self.extension / 'old-ui').exists())

    def test_foreign_connection_and_symlinks_fail_before_staging(self):
        deploy.atomic(self.config, b'{"mcpServers":{"workbuddy-cua":{"command":"foreign","args":["server.py"]}}}')
        original = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, '占用'):
            self.install(FakeService())
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.runtime / 'releases').exists())
        self.config.rename(self.config.with_name('real.json'))
        self.config.symlink_to('real.json')
        with self.assertRaisesRegex(ValueError, '符号链接'):
            self.install(FakeService())

    def test_legacy_connection_key_is_kept(self):
        legacy = self.original['mcpServers'].pop('workbuddy-cua')
        self.original['mcpServers']['cua'] = legacy
        deploy.atomic(self.config, json.dumps(self.original).encode())
        self.install(FakeService())
        servers = json.loads(self.config.read_bytes())['mcpServers']
        self.assertNotIn('workbuddy-cua', servers)
        self.assertIn('cua', servers)

    def transaction(self):
        directory = self.root / 'transaction'
        directory.mkdir()
        source = self.root / 'new-extension'
        source.mkdir()
        (source / 'new-ui').write_text('new')
        files = [(self.config, self.config.read_bytes(), b'new-config', 0o600)]
        return deploy.Transaction(directory, files, self.extension, source)

    def test_journal_recovers_interrupted_commit(self):
        original = self.config.read_bytes()
        transaction = self.transaction()
        transaction.apply()
        recovered = deploy.Transaction.reopen(transaction.directory)
        recovered.rollback()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertTrue((self.extension / 'old-ui').exists())
        recovered.rollback()  # Recovery is repeatable.

    def test_commit_and_recovery_protect_concurrent_changes(self):
        transaction = self.transaction()
        deploy.atomic(self.config, b'external-change')
        with self.assertRaisesRegex(ValueError, '其他程序修改'):
            transaction.apply()
        self.assertEqual(self.config.read_bytes(), b'external-change')
        # A recovery must also preserve edits made after a successful switch.
        deploy.atomic(self.config, transaction.files[0][1])
        transaction.apply()
        deploy.atomic(self.config, b'external-after-commit')
        with self.assertRaisesRegex(RuntimeError, '外部修改'):
            transaction.rollback()
        self.assertEqual(self.config.read_bytes(), b'external-after-commit')
        self.assertTrue((self.extension / 'old-ui').exists())
        deploy.atomic(self.config, b'new-config')
        transaction.rollback()
        self.assertEqual(transaction.values['phase'], 'rolled-back')

    def test_unfinished_journal_blocks_another_install(self):
        transaction = self.transaction()
        journal = self.runtime / 'transactions/interrupted/journal.json'
        deploy.atomic(journal, transaction.journal.read_bytes())
        with self.assertRaisesRegex(ValueError, '未完成安装'):
            self.install(FakeService())


if __name__ == '__main__':
    unittest.main()
