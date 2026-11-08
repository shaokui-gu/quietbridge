import importlib.util
import hashlib
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
sys.path.insert(0, str(ROOT / 'tools'))
from release_layout import copy_bridge


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
        self.binary = deploy.APP / 'Contents/MacOS/quietbridge-native'
        self.old_binary = deploy.LEGACY_APP / 'Contents/MacOS/quietbridge-native'
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
            (Path('/tmp/quietbridge-native'), self.owner, str(self.old_binary)),
            (self.binary, self.owner, str(self.old_binary) + ' --shared-server'),
            (self.binary, self.owner, str(self.old_binary) + ' --menubar --unexpected'),
            (self.binary, self.owner, '/tmp/QuietBridge.app/Contents/MacOS/quietbridge-native'),
        ]
        for executable, owner, arguments in cases:
            with self.subTest(executable=executable, owner=owner, arguments=arguments):
                self.assertFalse(self.service.owns_process(executable, owner, arguments))

    def test_versioned_processes_require_their_own_launch_path(self):
        release = self.home / 'Library/Application Support/QuietBridge/releases'
        binary = release / 'old/QuietBridge.app/Contents/MacOS/quietbridge-native'
        self.assertTrue(self.service.owns_process(binary, self.owner, str(binary) + ' --menubar'))
        self.assertFalse(self.service.owns_process(binary, self.owner, str(self.old_binary)))
        legacy = release / 'old/workbuddy-cua.app/Contents/MacOS/workbuddy-cua-native'
        self.assertTrue(self.service.owns_process(legacy, self.owner, str(legacy) + ' --menubar'))
        other = release / 'other/QuietBridge.app/Contents/MacOS/quietbridge-native'
        self.assertFalse(self.service.owns_process(binary, self.owner, str(other)))


class LifecycleHookPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve() / 'home with spaces'
        (self.home / '.workbuddy').mkdir(parents=True)
        self.settings = self.home / '.workbuddy/settings.json'
        self.connector = self.home / 'Library/Application Support/QuietBridge/connector/connector.py'
        self.config = json.dumps({'mcpServers': {'quietbridge': {'args': ['-B', str(self.connector)], 'env': {}}}}).encode()

    def test_merge_is_idempotent_and_preserves_unrelated_hooks_and_settings(self):
        foreign = {'matcher': 'Read', 'hooks': [{'type': 'command', 'command': 'existing-hook'}]}
        self.settings.write_text(json.dumps({'custom': 7, 'hooks': {'PostToolUse': [foreign],
            'Stop': [{'hooks': [{'type': 'command', 'command': 'keep-stop'}]}]}}))
        path, original, rendered = deploy.lifecycle_plan(self.home, self.connector, self.config)
        self.assertEqual(path, self.settings)
        self.assertEqual(original, self.settings.read_bytes())
        result = json.loads(rendered)
        self.assertEqual(result['custom'], 7)
        self.assertEqual(result['hooks']['PostToolUse'][0], foreign)
        self.assertNotIn('matcher', result['hooks']['PostToolUse'][-1])
        self.assertEqual(result['hooks']['Stop'][0]['hooks'][0]['command'], 'keep-stop')
        for event in ['PostToolUse', 'FinalStop', 'SessionEnd']:
            command = result['hooks'][event][-1]['hooks'][0]['command']
            parts = deploy.shlex.split(command)
            self.assertEqual(parts[2], str(self.connector.parent / 'adapters/workbuddy/lifecycle.py'))
            self.assertEqual(parts[-2], '--config-dir')
        self.settings.write_bytes(rendered)
        self.assertEqual(deploy.lifecycle_plan(self.home, self.connector, self.config)[2], rendered)

    def test_existing_diagnostics_config_path_is_used(self):
        data = json.loads(self.config)
        data['mcpServers']['quietbridge']['env']['QUIETBRIDGE_CONFIG_DIR'] = str(self.home / 'custom runtime')
        result = json.loads(deploy.lifecycle_plan(self.home, self.connector, json.dumps(data).encode())[2])
        command = result['hooks']['FinalStop'][0]['hooks'][0]['command']
        self.assertEqual(deploy.shlex.split(command)[-1], str(self.home / 'custom runtime'))

    def test_invalid_settings_are_rejected_without_replacement(self):
        self.settings.write_text('{"hooks": []}')
        before = self.settings.read_bytes()
        with self.assertRaises(ValueError): deploy.lifecycle_plan(self.home, self.connector, self.config)
        self.assertEqual(self.settings.read_bytes(), before)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.runtime = self.home / 'Library/Application Support/QuietBridge'
        self.config = self.home / '.workbuddy/mcp.json'
        self.agent = self.home / 'Library/LaunchAgents' / (deploy.LABEL + '.plist')
        self.extension = self.runtime / 'extension/extension-stage'
        self.app = self.root / 'menu.app'
        (self.app / 'Contents/MacOS').mkdir(parents=True)
        (self.app / 'Contents/MacOS/quietbridge-native').write_bytes(b'new-menu')
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '0.5.27', 'CFBundleVersion': '55'}))
        bridge = self.app / 'Contents/Resources/bridge'
        bridge.mkdir(parents=True)
        (bridge / 'server.py').write_text('import json; print(json.dumps({"result":{"serverInfo":{"name":"quietbridge"}}}))')
        copy_bridge(ROOT, bridge)
        self.support = self.root / 'support'
        extension = self.support / 'extension/extension-stage'
        extension.mkdir(parents=True)
        for name in ('manifest.json', 'native_host.py', 'install_native_host.py'):
            (extension / name).write_bytes((ROOT / 'extensions/chromium' / name).read_bytes())
        (extension / 'browser_events.py').write_bytes((ROOT / 'services/mcp/browser_events.py').read_bytes())
        (extension / 'compatibility.py').write_bytes((ROOT / 'services/mcp/compatibility.py').read_bytes())
        for module in ('session_registry.py', 'lifecycle_store.py'):
            (extension / module).write_bytes((ROOT / 'services/mcp' / module).read_bytes())
        (extension / 'popup.html').write_text('new-ui')
        helper_app = self.root / 'helper.app'
        (helper_app / 'Contents/MacOS').mkdir(parents=True)
        (helper_app / 'Contents/Info.plist').write_bytes((self.app / 'Contents/Info.plist').read_bytes())
        self.helper = helper_app / 'Contents/MacOS/quietbridge-native'
        self.helper.write_bytes(b'independent-helper')
        self.original = {'mcpServers': {'other': {'command': 'untouched'}, 'quietbridge': {
            'command': 'old-python', 'args': ['-B', str(deploy.APP / 'Contents/Resources/bridge/server.py')],
            'env': {'QUIETBRIDGE_HELPER_PATH': 'old-public-helper', 'KEEP_ME': 'value'}, 'custom': True}}}
        deploy.atomic(self.config, json.dumps(self.original).encode())
        deploy.atomic(self.agent, plistlib.dumps({'Label': deploy.LABEL, 'ProgramArguments': [str(deploy.APP / 'Contents/MacOS/quietbridge-native'), '--menubar']}), 0o644)
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
        self.assertEqual(deploy.APP.name, 'QuietBridge.app')
        self.assertEqual(deploy.LEGACY_APP.name, 'workbuddy-cua.app')
        entry = {'args': [str(deploy.LEGACY_APP / 'Contents/Resources/bridge/server.py')]}
        self.assertTrue(deploy.owned(entry, self.home))

    def test_cached_old_bridge_requires_refresh_even_after_config_migrated(self):
        self.install(FakeService())
        with patch.object(deploy, 'legacy_bridges', return_value=[{'pid': 12345, 'bridge': 'old/server.py'}]):
            result = self.install(FakeService())
        self.assertEqual(result['connectorUpdate']['state'], 'host_refresh_required')
        self.assertEqual(result['connectorUpdate']['legacyBridgePids'], [12345])

    def test_published_bootstrap_bytes_survive_a_branding_upgrade(self):
        self.install(FakeService())
        connector = self.runtime / 'connector/connector.py'
        previous = b'# isolated published transport fixture\n'
        connector.write_bytes(previous)
        with patch.object(deploy, 'PUBLISHED_CONNECTOR_DIGESTS', {hashlib.sha256(previous).hexdigest()}):
            self.install(FakeService())
        self.assertEqual(connector.read_bytes(), previous)

    def test_changed_unknown_bootstrap_reports_host_trust_revalidation(self):
        self.install(FakeService())
        connector = self.runtime / 'connector/connector.py'
        connector.write_bytes(b'# unknown transport fixture\n')
        result = self.install(FakeService())
        self.assertEqual(result['connectorUpdate']['state'], 'host_refresh_required')
        self.assertTrue(result['connectorUpdate']['transportChanged'])

    def test_rename_upgrade_keeps_legacy_connector_trust_state_and_browser_paths(self):
        legacy = self.home / 'Library/Application Support/workbuddy-cua'
        self.runtime.rename(legacy)
        self.runtime = legacy
        self.extension = legacy / 'extension/extension-stage'
        connector = legacy / 'connector/connector.py'
        browser = self.home / 'Library/Application Support/WorkbuddyExtension'
        browser.mkdir()
        config = self.original['mcpServers'].pop('quietbridge')
        config.update(args=['-B', str(connector)], disabled=True)
        config['env'] = {'CUA_CONFIG_DIR': str(browser / 'cua-helper-config'),
                         'CUA_HELPER_PATH': 'previous-helper', 'KEEP_ME': 'value'}
        self.original['mcpServers']['workbuddy-cua'] = config
        deploy.atomic(self.config, json.dumps(self.original).encode())
        deploy.atomic(self.agent, plistlib.dumps({'Label': deploy.LABEL, 'ProgramArguments': [
            '/Applications/Workbuddy CUA.app/Contents/MacOS/workbuddy-cua-native', '--menubar']}))
        old_hook = {'hooks': [{'type': 'command', 'command': ' '.join(deploy.shlex.quote(str(x)) for x in
                    (sys.executable, '-B', connector.with_name('host_lifecycle.py')))}]}
        settings = self.home / '.workbuddy/settings.json'
        deploy.atomic(settings, json.dumps({'hooks': {'FinalStop': [old_hook]}}).encode())
        outcome = self.install(FakeService())
        self.assertTrue(outcome['installed'])
        servers = json.loads(self.config.read_bytes())['mcpServers']
        self.assertNotIn('quietbridge', servers)
        self.assertTrue(servers['workbuddy-cua']['disabled'])
        self.assertEqual(servers['workbuddy-cua']['args'][-1], str(connector))
        self.assertEqual(servers['workbuddy-cua']['env'],
                         {'CUA_CONFIG_DIR': str(browser / 'cua-helper-config'), 'KEEP_ME': 'value'})
        hooks = json.loads(settings.read_bytes())['hooks']['FinalStop']
        self.assertEqual(len(hooks), 1)
        self.assertIn('adapters/workbuddy/lifecycle.py', hooks[0]['hooks'][0]['command'])
        registration = self.home / 'Library/Application Support/Google/Chrome/NativeMessagingHosts/com.cua.browser_observer.json'
        self.assertEqual(json.loads(registration.read_bytes())['path'],
                         str(browser / 'browser-observer-extension/native-host-launcher.sh'))
        self.assertTrue((browser / 'cua-mcp-bridge/compatibility.py').is_file())
        self.assertFalse((self.home / 'Library/Application Support/QuietBridgeBrowser').exists())

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
        entry = data['mcpServers']['quietbridge']
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
        settings = json.loads((self.home / '.workbuddy/settings.json').read_text())
        self.assertEqual(set(settings['hooks']), {'PreToolUse', 'PostToolUse', 'FinalStop', 'SessionEnd'})
        self.assertEqual((self.runtime / 'connector/adapters/workbuddy/lifecycle.py').read_bytes(),
                         (ROOT / 'adapters/workbuddy/lifecycle.py').read_bytes())
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
        self.assertFalse((self.home / 'Library/Application Support/QuietBridgeBrowser/extensions/chromium/native_host.py').exists())
        self.assertEqual(service.starts, 1)  # Restore only the previous agent.
        journal = next((self.runtime / 'transactions').glob('*/journal.json'))
        self.assertEqual(json.loads(journal.read_bytes())['phase'], 'rolled-back')
        backups = list(journal.parent.glob('backup-*'))
        self.assertTrue(backups)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in backups))

    def test_upgrade_restores_running_menu_and_keeps_disabled_connector(self):
        self.original['mcpServers']['quietbridge']['disabled'] = True
        deploy.atomic(self.config, json.dumps(self.original).encode())
        service = FakeService(running=True)
        result = self.install(service)
        self.assertTrue(result['startupVerified'])
        self.assertEqual(service.starts, 1)
        self.assertTrue(json.loads(self.config.read_bytes())['mcpServers']['quietbridge']['disabled'])

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
    return {'id': m.get('id'), 'result': {'serverInfo': {'name': 'quietbridge'}, 'capabilities': {'tools': {}}}}
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
        deploy.atomic(self.config, b'{"mcpServers":{"quietbridge":{"command":"foreign","args":["server.py"]}}}')
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
        legacy = self.original['mcpServers'].pop('quietbridge')
        self.original['mcpServers']['cua'] = legacy
        deploy.atomic(self.config, json.dumps(self.original).encode())
        self.install(FakeService())
        servers = json.loads(self.config.read_bytes())['mcpServers']
        self.assertNotIn('quietbridge', servers)
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
        self.old = self.public / 'old/QuietBridge.app/Contents/MacOS/quietbridge-native'
        self.new = self.public / 'new/QuietBridge.app/Contents/MacOS/quietbridge-native'
        for executable, build in ((self.old, '75'), (self.new, '79')):
            executable.parent.mkdir(parents=True)
            executable.write_text('fixture')
            (executable.parents[2] / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': 'com.workbuddy.cua.helper', 'CFBundleVersion': build}))
        patcher = patch.object(deploy, 'PUBLIC', self.public)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_upgrade_retires_only_signed_old_quietbridge_helpers(self):
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
        bridge = home / 'Library/Application Support/QuietBridge/releases/old/QuietBridge.app/Contents/Resources/bridge/server.py'
        (bridge.parents[3] / 'Contents').mkdir(parents=True)
        (bridge.parents[3] / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': deploy.LEGACY_MENU_IDENTIFIER}))
        reply = deploy.subprocess.CompletedProcess([], 0, str(executable) + ' -B ' + str(bridge) + '\n', '')
        with patch.object(deploy, 'user_processes', return_value=[(11, executable)]), \
                patch.object(deploy.subprocess, 'run', return_value=reply), patch.object(deploy, 'verify_code'):
            self.assertEqual(deploy.legacy_bridges(home), [{'pid': 11, 'bridge': str(bridge)}])

    def test_generic_helper_cleanup_verifies_its_exact_new_identity(self):
        info = self.old.parents[2] / 'Contents/Info.plist'
        info.write_bytes(plistlib.dumps({'CFBundleIdentifier': deploy.HELPER_IDENTIFIER, 'CFBundleVersion': '75'}))
        active = [(11, self.old)]
        def stop(pid, sig):
            active.clear()
        with patch.object(deploy, 'user_processes', side_effect=lambda: list(active)), \
                patch.object(deploy, 'verify_code') as verify, patch.object(deploy.os, 'kill', side_effect=stop):
            self.assertEqual(deploy.retire_previous_helpers(self.new, '83'), [11])
        verify.assert_called_once_with(self.old.parents[2], deploy.HELPER_IDENTIFIER)

    def test_generic_migration_does_not_retire_an_unrecognised_identity(self):
        info = self.old.parents[2] / 'Contents/Info.plist'
        info.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.other.helper', 'CFBundleVersion': '75'}))
        with patch.object(deploy, 'user_processes', return_value=[(11, self.old)]), \
                patch.object(deploy, 'verify_code') as verify, patch.object(deploy.os, 'kill') as kill:
            self.assertEqual(deploy.retire_previous_helpers(self.new, '83'), [])
        verify.assert_not_called()
        kill.assert_not_called()
