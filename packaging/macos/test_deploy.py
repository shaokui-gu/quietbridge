import importlib.util
import json
import os
from pathlib import Path
import plistlib
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch

SPEC = importlib.util.spec_from_file_location('package_deploy', Path(__file__).with_name('deploy.py'))
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)
ROOT = Path(__file__).resolve().parents[2]


class FakeService:
    def __init__(self, fail=False, running=False):
        self.starts = 0
        self.stops = 0
        self.fail = fail
        self.running = running

    def is_running(self):
        return self.running

    def stop(self):
        self.stops += 1

    def start(self):
        self.starts += 1

    def wait_ready(self, executable, info):
        if self.fail:
            raise RuntimeError('simulated startup failure')
        if info['CFBundleVersion'] != '55' or not executable.is_file():
            raise RuntimeError('wrong deployed version')


class MenuBarServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.service = deploy.MenuBarService(self.home)
        self.binary = deploy.APP / 'Contents/MacOS/workbuddy-cua-native'
        self.old_binary = deploy.LEGACY_APP / 'Contents/MacOS/workbuddy-cua-native'
        self.owner = str(os.getuid())

    def test_stop_handles_bundle_rename_with_original_launch_arguments(self):
        pid = 12345
        library = Mock()

        def process_path(process, buffer, size):
            self.assertEqual(process, pid)
            buffer.value = os.fsencode(self.binary)
            return len(buffer.value)

        library.proc_pidpath.side_effect = process_path
        for suffix in ('', ' --menubar', ' -psn_0_12345'):
            with self.subTest(arguments=suffix):
                def run(arguments, **kwargs):
                    if arguments[:2] == ['/bin/launchctl', 'print']:
                        return deploy.subprocess.CompletedProcess(arguments, 1, b'', b'Could not find service')
                    if arguments[0] == '/usr/sbin/lsof':
                        return deploy.subprocess.CompletedProcess(arguments, 0, str(pid) + '\n', '')
                    if arguments[0] == '/bin/ps':
                        value = self.owner if arguments[-1] == 'uid=' else str(self.old_binary) + suffix
                        return deploy.subprocess.CompletedProcess(arguments, 0, value + '\n', '')
                    self.fail('unexpected command: ' + repr(arguments))

                with patch.object(deploy.subprocess, 'run', side_effect=run), \
                     patch.object(deploy.ctypes, 'CDLL', return_value=library), \
                     patch.object(deploy.os, 'kill', side_effect=[None, ProcessLookupError]) as kill:
                    self.service.stop()
                self.assertEqual(kill.call_args_list, [call(pid, deploy.signal.SIGTERM), call(pid, 0)])

    def test_foreign_processes_and_non_menu_arguments_are_rejected(self):
        cases = [
            (self.binary, str(os.getuid() + 1), str(self.old_binary)),
            (Path('/tmp/workbuddy-cua-native'), self.owner, str(self.old_binary)),
            (self.binary, self.owner, str(self.old_binary) + ' --shared-server'),
            (self.binary, self.owner, str(self.old_binary) + ' --menubar --unexpected'),
            (self.binary, self.owner, '/tmp/Workbuddy CUA.app/Contents/MacOS/workbuddy-cua-native'),
        ]
        for executable, owner, arguments in cases:
            with self.subTest(executable=executable, owner=owner, arguments=arguments):
                self.assertFalse(self.service.owns_process(executable, owner, arguments))

    def test_versioned_processes_require_their_own_launch_path(self):
        release = self.home / 'Library/Application Support/workbuddy-cua/releases'
        binary = release / 'old/workbuddy-cua.app/Contents/MacOS/workbuddy-cua-native'
        self.assertTrue(self.service.owns_process(binary, self.owner, str(binary) + ' --menubar'))
        self.assertFalse(self.service.owns_process(binary, self.owner, str(self.old_binary)))
        other = release / 'other/workbuddy-cua.app/Contents/MacOS/workbuddy-cua-native'
        self.assertFalse(self.service.owns_process(binary, self.owner, str(other)))


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
        (bridge / 'connector.py').write_bytes((ROOT / 'cua-mcp-bridge/connector.py').read_bytes())
        (bridge / 'stdio_transport.py').write_bytes((ROOT / 'cua-mcp-bridge/stdio_transport.py').read_bytes())
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
        process_mock = patch.object(deploy, 'user_processes', return_value=[])
        process_mock.start()
        self.addCleanup(process_mock.stop)
        mock = patch.object(deploy.platform, 'mac_ver', return_value=('14.0', ('', '', ''), ''))
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(deploy.platform, 'machine', return_value='arm64')
        mock.start()
        self.addCleanup(mock.stop)

    def install(self, service):
        return deploy.deploy(self.home, self.helper, self.app, self.support, service)

    def test_old_visible_app_path_remains_a_recognised_connection(self):
        self.assertEqual(deploy.APP.name, 'Workbuddy CUA.app')
        self.assertEqual(deploy.LEGACY_APP.name, 'workbuddy-cua.app')
        entry = {'args': [str(deploy.LEGACY_APP / 'Contents/Resources/bridge/server.py')]}
        self.assertTrue(deploy.owned(entry, self.home))

    def test_cached_old_bridge_requires_refresh_even_after_config_migrated(self):
        self.install(FakeService())
        with patch.object(deploy, 'legacy_bridges', return_value=[{'pid': 12345, 'bridge': 'old/server.py'}]):
            result = self.install(FakeService())
        self.assertEqual(result['connectorUpdate']['state'], 'host_refresh_required')
        self.assertEqual(result['connectorUpdate']['legacyBridgePids'], [12345])

    def test_failed_process_audit_cannot_claim_connection_reloaded(self):
        self.install(FakeService())
        with patch.object(deploy, 'user_processes', side_effect=PermissionError('fixture')):
            result = self.install(FakeService())
        self.assertTrue(result['installed'])
        self.assertEqual(result['connectorUpdate']['state'], 'verification_unavailable')

    def test_success_preserves_settings_and_uses_independent_helper(self):
        service = FakeService()
        outcome = self.install(service)
        data = json.loads(self.config.read_bytes())
        self.assertEqual(data['mcpServers']['other'], self.original['mcpServers']['other'])
        entry = data['mcpServers']['workbuddy-cua']
        self.assertEqual(entry['env'], {'KEEP_ME': 'value'})
        self.assertEqual(entry['args'][-1], str(self.runtime / 'connector/connector.py'))
        manifest = json.loads((self.runtime / 'connector/active.json').read_bytes())
        self.assertEqual(manifest['helper'], str(self.helper))
        self.assertTrue(Path(manifest['bridge']).is_file())
        self.assertEqual(outcome['connectorUpdate']['state'], 'host_refresh_required')
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
        self.assertEqual(again['connectorUpdate']['state'], 'pending_connection')
        self.assertEqual(self.config.read_bytes(), json.dumps(data, ensure_ascii=False, indent=2).encode() + b'\n')
        self.assertEqual(again['helper'], outcome['helper'])
        self.assertEqual(len(list((self.runtime / 'releases').glob('[!.]*'))), 1)

    def test_commit_failure_restores_all_user_components(self):
        original, agent = self.config.read_bytes(), self.agent.read_bytes()
        service = FakeService(running=True)
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

    def test_upgrade_restores_running_menu_and_keeps_disabled_connector(self):
        self.original['mcpServers']['workbuddy-cua']['disabled'] = True
        deploy.atomic(self.config, json.dumps(self.original).encode())
        service = FakeService(running=True)
        result = self.install(service)
        self.assertTrue(result['startupVerified'])
        self.assertEqual(service.starts, 1)
        self.assertTrue(json.loads(self.config.read_bytes())['mcpServers']['workbuddy-cua']['disabled'])

    def test_startup_failure_restores_active_release_and_config(self):
        self.install(FakeService())
        manifest = (self.runtime / 'connector/active.json').read_bytes()
        config = self.config.read_bytes()
        service = FakeService(fail=True, running=True)
        with self.assertRaisesRegex(RuntimeError, 'startup failure'):
            self.install(service)
        self.assertEqual(self.config.read_bytes(), config)
        self.assertEqual((self.runtime / 'connector/active.json').read_bytes(), manifest)

    def test_running_connector_acknowledges_real_transaction_without_host_restart(self):
        bridge = self.app / 'Contents/Resources/bridge/server.py'
        bridge.write_text('''import json, sys
class Helper:
    def _stop(self): pass
HELPER = Helper()
def handle(m):
    if m.get('method', '').startswith('notifications/'): return None
    return {'id': m.get('id'), 'result': {'serverInfo': {'name': 'workbuddy-cua'}, 'capabilities': {'tools': {}}}}
if __name__ == '__main__':
    for line in sys.stdin: print(json.dumps(handle(json.loads(line))), flush=True)
''')
        self.install(FakeService())
        original_config = self.config.read_bytes()
        process = subprocess.Popen([sys.executable, '-B', str(self.runtime / 'connector/connector.py')],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def close():
            process.stdin.close()
            try: process.wait(timeout=4)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(close)
        replies = queue.Queue()
        def reader():
            for line in process.stdout: replies.put(json.loads(line))
        threading.Thread(target=reader, daemon=True).start()
        process.stdin.write('{"id": 1, "method": "initialize"}\n')
        process.stdin.flush()
        self.assertTrue(replies.get(timeout=4)['result']['capabilities']['tools']['listChanged'])
        result = self.install(FakeService(running=True))
        self.assertEqual(result['connectorUpdate']['state'], 'reloaded')
        self.assertEqual(self.config.read_bytes(), original_config)
        self.assertEqual(replies.get(timeout=4)['method'], 'notifications/tools/list_changed')
        self.assertIsNone(process.poll())

    def test_first_install_keeps_app_stopped_and_reports_initial_setup(self):
        self.config.unlink()
        self.agent.unlink()
        service = FakeService()
        result = self.install(service)
        self.assertEqual(result['connectorUpdate']['state'], 'first_install')
        self.assertEqual(service.starts, 0)

    def test_corrupt_runtime_status_cannot_fail_a_committed_installation(self):
        self.install(FakeService())
        (self.runtime / 'connector/status.json').write_text('broken runtime acknowledgment')
        result = self.install(FakeService())
        self.assertTrue(result['installed'])
        self.assertEqual(result['connectorUpdate']['state'], 'verification_unavailable')
        self.assertEqual(json.loads(Path(result['journal']).read_bytes())['phase'], 'committed')

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


class LegacyRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.public = self.root / 'helpers'
        self.old = self.public / 'old/workbuddy-cua.app/Contents/MacOS/workbuddy-cua-native'
        self.new = self.public / 'new/workbuddy-cua.app/Contents/MacOS/workbuddy-cua-native'
        for executable, build in ((self.old, '75'), (self.new, '79')):
            executable.parent.mkdir(parents=True)
            executable.write_text('fixture')
            (executable.parents[2] / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': 'com.workbuddy.cua.helper', 'CFBundleVersion': build}))
        patcher = patch.object(deploy, 'PUBLIC', self.public)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_upgrade_retires_only_signed_old_cua_helpers(self):
        active = [(11, self.old), (12, self.new), (13, Path('/Applications/Calculator.app/Contents/MacOS/Calculator'))]
        def stop(pid, sig):
            self.assertEqual((pid, sig), (11, deploy.signal.SIGTERM))
            active[:] = [item for item in active if item[0] != pid]
        with patch.object(deploy, 'user_processes', side_effect=lambda: list(active)), \
                patch.object(deploy, 'verify_code') as verify, patch.object(deploy.os, 'kill', side_effect=stop):
            self.assertEqual(deploy.retire_previous_helpers(self.new, '79'), [11])
        verify.assert_called_once_with(self.old.parents[2], 'com.workbuddy.cua.helper')
        self.assertEqual([pid for pid, _ in active], [12, 13])

    def test_signature_failure_cannot_signal_a_process(self):
        with patch.object(deploy, 'user_processes', return_value=[(11, self.old)]), \
                patch.object(deploy, 'verify_code', side_effect=ValueError('wrong signer')), \
                patch.object(deploy.os, 'kill') as kill:
            with self.assertRaises(ValueError): deploy.retire_previous_helpers(self.new, '79')
            kill.assert_not_called()

    def test_pid_reuse_with_another_executable_is_not_signaled(self):
        with patch.object(deploy, 'user_processes', side_effect=[[(11, self.old)], [], [], []]), \
                patch.object(deploy, 'verify_code'), patch.object(deploy.os, 'kill') as kill:
            self.assertEqual(deploy.retire_previous_helpers(self.new, '79'), [11])
            kill.assert_not_called()

    def test_legacy_audit_recognizes_a_running_cached_script(self):
        home = self.root / 'home'
        executable = Path('/usr/bin/python3')
        bridge = home / 'Library/Application Support/workbuddy-cua/releases/old/workbuddy-cua.app/Contents/Resources/bridge/server.py'
        reply = deploy.subprocess.CompletedProcess([], 0, str(executable) + ' -B ' + str(bridge) + '\n', '')
        with patch.object(deploy, 'user_processes', return_value=[(11, executable)]), \
                patch.object(deploy.subprocess, 'run', return_value=reply), patch.object(deploy, 'verify_code'):
            self.assertEqual(deploy.legacy_bridges(home), [{'pid': 11, 'bridge': str(bridge)}])
