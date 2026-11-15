import importlib.util
import hashlib
import json
import os
from pathlib import Path
import plistlib
import queue
import re
import shutil
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
from compatibility import browser_layout


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
        self.running = False

    def start(self):
        self.starts += 1
        self.running = True

    def wait_ready(self, executable, info):
        if self.fail:
            raise RuntimeError('simulated startup failure')
        if info['CFBundleVersion'] != '55' or not executable.is_file():
            raise RuntimeError('wrong deployed version')


class UpgradeService(FakeService):
    def __init__(self, binary, running=True, fail_new=False):
        super().__init__(running=running)
        self.binary = binary
        self.fail_new = fail_new
        self.observed = []

    def processes(self):
        return [(12345, self.binary)] if self.running else []

    def wait_ready(self, executable, info):
        self.observed.append((info['CFBundleVersion'], executable.read_bytes()))
        if self.fail_new and info['CFBundleVersion'] == '55':
            raise RuntimeError('simulated new version startup failure')


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
                     patch.object(deploy, 'user_processes', return_value=[(pid, self.binary)]), \
                     patch.object(deploy.Path, 'read_bytes', return_value=plistlib.dumps({'CFBundleIdentifier': deploy.MENU_IDENTIFIER})), \
                     patch.object(deploy, 'verify_code') as verify:
                    self.assertEqual(self.service.processes(), [(pid, self.binary)])
                    verify.assert_called_once()

    def test_stop_waits_for_bootout_and_handles_a_menu_without_a_status_port(self):
        processes = [(12345, self.binary)]
        def run(arguments, **kwargs):
            if arguments[0] == '/usr/sbin/lsof':
                return subprocess.CompletedProcess(arguments, 1, '', '')
            return subprocess.CompletedProcess(arguments, 0, b'', b'')
        with patch.object(self.service, 'processes', side_effect=[processes, processes, processes, []]), \
             patch.object(deploy.subprocess, 'run', side_effect=run), \
             patch.object(deploy.os, 'kill') as kill, patch.object(deploy.time, 'sleep'):
            self.service.stop()
        kill.assert_called_once_with(12345, deploy.signal.SIGTERM)

    def test_an_unresponsive_menu_prevents_payload_replacement_without_force_killing(self):
        processes = [(12345, self.binary)]
        def run(arguments, **kwargs):
            if arguments[0] == '/usr/sbin/lsof':
                return subprocess.CompletedProcess(arguments, 1, '', '')
            return subprocess.CompletedProcess(arguments, 1, b'', b'Could not find service')
        with patch.object(self.service, 'processes', return_value=processes), \
             patch.object(deploy.subprocess, 'run', side_effect=run), \
             patch.object(deploy.time, 'monotonic', side_effect=[0, 6]), \
             patch.object(deploy.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, '未允许替换'):
                self.service.stop()
        kill.assert_called_once_with(12345, deploy.signal.SIGTERM)

    def test_foreign_port_owner_is_rejected_before_unloading_or_signaling(self):
        with patch.object(self.service, 'processes', return_value=[]), \
             patch.object(deploy.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '9999\n', '')) as run, \
             patch.object(deploy.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, '其他程序'):
                self.service.stop()
        self.assertEqual(run.call_count, 1)
        kill.assert_not_called()

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
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '0.5.27', 'CFBundleVersion': '55',
            'CFBundleIdentifier': deploy.MENU_IDENTIFIER, 'CFBundleExecutable': 'quietbridge-native'}))
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
        for module in ('session_registry.py', 'lifecycle_store.py', 'browser_preview.py'):
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

    def prepare_upgrade(self, running=True, fail_new=False):
        info_path = self.app / 'Contents/Info.plist'
        new_info = info_path.read_bytes()
        old_info = plistlib.loads(new_info)
        old_info.update(CFBundleShortVersionString='0.5.26', CFBundleVersion='54')
        info_path.write_bytes(plistlib.dumps(old_info))
        binary = self.app / 'Contents/MacOS/quietbridge-native'
        binary.write_bytes(b'old-version-before-payload')
        service = UpgradeService(binary, running=running, fail_new=fail_new)
        state = deploy.prepare_install(self.home, '0.5.27', '55', service)
        self.assertEqual(binary.read_bytes(), b'old-version-before-payload')
        self.assertFalse(service.running, 'preinstall must finish stopping before payload replacement')
        # Model PackageKit replacing the source after preinstall returns.
        info_path.write_bytes(new_info)
        binary.write_bytes(b'new-version-payload')
        return service, state

    def test_preinstall_stop_state_survives_payload_and_restarts_only_the_new_version(self):
        service, state = self.prepare_upgrade()
        self.assertEqual(state['phase'], 'stopped')
        self.assertTrue(state['wasRunning'])
        self.assertEqual(Path(state['restoreBinary']).read_bytes(), b'old-version-before-payload')
        result = deploy.finish_install(self.home, self.helper, self.app, self.support, service)
        self.assertTrue(result['stoppedBeforePayload'])
        self.assertTrue(result['startupVerified'])
        self.assertEqual(service.starts, 1)
        self.assertEqual(service.observed, [('55', b'new-version-payload')])
        self.assertEqual(deploy.read_upgrade(self.home)['phase'], 'committed')

    def test_preinstall_preserves_a_previously_stopped_application(self):
        service, _ = self.prepare_upgrade(running=False)
        result = deploy.finish_install(self.home, self.helper, self.app, self.support, service)
        self.assertFalse(result['startupVerified'])
        self.assertEqual(service.starts, 0)
        self.assertEqual(deploy.read_upgrade(self.home)['phase'], 'committed')

    def test_deployment_failure_before_file_transaction_restores_independent_old_copy(self):
        service, state = self.prepare_upgrade()
        with patch.object(deploy, 'handshake', side_effect=RuntimeError('fixture handshake failure')):
            with self.assertRaisesRegex(RuntimeError, '已恢复安装前'):
                deploy.finish_install(self.home, self.helper, self.app, self.support, service)
        self.assertEqual(service.observed, [('54', b'old-version-before-payload')])
        self.assertEqual(deploy.read_upgrade(self.home)['phase'], 'rolled-back')
        self.assertEqual(plistlib.loads(self.agent.read_bytes())['ProgramArguments'], [state['restoreBinary'], '--menubar'])
        self.assertEqual((self.app / 'Contents/MacOS/quietbridge-native').read_bytes(), b'new-version-payload',
                         'user rollback must not pretend to restore root-owned PackageKit payloads')

    def test_new_startup_failure_rolls_back_files_before_resuming_original_version(self):
        original = self.config.read_bytes()
        service, _ = self.prepare_upgrade(fail_new=True)
        with self.assertRaisesRegex(RuntimeError, 'startup failure'):
            deploy.finish_install(self.home, self.helper, self.app, self.support, service)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(service.observed, [('55', b'new-version-payload'), ('54', b'old-version-before-payload')])
        self.assertEqual(deploy.read_upgrade(self.home)['phase'], 'rolled-back')

    def test_recovery_handles_payload_interruption_without_postinstall_or_user_transaction(self):
        service, state = self.prepare_upgrade()
        self.assertFalse((self.runtime / 'transactions').exists())
        recovery = deploy.upgrade_recovery_command(self.home, state)
        self.assertTrue(recovery.is_file())
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o700)
        self.assertEqual((recovery.parent / 'deploy.py').read_bytes(), Path(deploy.__file__).read_bytes())
        self.assertTrue((recovery.parent / 'compatibility.py').is_file())
        self.assertTrue((recovery.parent / 'adapters/workbuddy/configuration.py').is_file())
        with self.assertRaisesRegex(ValueError, '替换文件前已暂停'):
            deploy.preflight(self.home)
        self.assertTrue(deploy.recover_latest(self.home, service))
        self.assertEqual(service.observed, [('54', b'old-version-before-payload')])
        self.assertEqual(deploy.read_upgrade(self.home)['phase'], 'rolled-back')
        deploy.preflight(self.home)
        self.assertFalse(deploy.recover_latest(self.home, service))

    def test_finder_launched_old_app_without_launch_agent_can_be_restored_after_replacement(self):
        self.agent.unlink()
        service, _ = self.prepare_upgrade()
        self.assertTrue(deploy.recover_latest(self.home, service))
        self.assertEqual(service.observed, [('54', b'old-version-before-payload')])
        self.assertTrue(self.agent.is_file())

    def test_interrupted_final_receipt_write_does_not_roll_back_a_committed_new_release(self):
        service, _ = self.prepare_upgrade()
        deploy.finish_install(self.home, self.helper, self.app, self.support, service)
        state = deploy.read_upgrade(self.home)
        state['phase'] = 'deploying'
        deploy.save_upgrade(self.home, state)
        stopped = service.stops
        self.assertTrue(deploy.recover_latest(self.home, service))
        self.assertEqual(service.stops, stopped)
        self.assertEqual(service.observed, [('55', b'new-version-payload')])
        self.assertEqual(deploy.read_upgrade(self.home)['phase'], 'committed')

    def test_recovery_preserves_intervening_launch_agent_edits(self):
        service, state = self.prepare_upgrade()
        modified = self.agent.read_bytes() + b'\n'
        self.agent.write_bytes(modified)
        with self.assertRaisesRegex(ValueError, '安装期间被修改'):
            deploy.restore_upgrade(self.home, state, service)
        self.assertEqual(self.agent.read_bytes(), modified)
        self.assertEqual(service.starts, 0)
        self.assertEqual(deploy.read_upgrade(self.home)['phase'], 'recovery-required')

    def test_pending_upgrade_cannot_be_overwritten_or_claimed_by_another_package(self):
        service, _ = self.prepare_upgrade()
        original = deploy.upgrade_path(self.home).read_bytes()
        with self.assertRaisesRegex(ValueError, '已暂停'):
            deploy.prepare_install(self.home, '0.5.28', '56', service)
        info_path = self.app / 'Contents/Info.plist'
        value = plistlib.loads(info_path.read_bytes())
        value['CFBundleVersion'] = '56'
        info_path.write_bytes(plistlib.dumps(value))
        with self.assertRaisesRegex(ValueError, '匹配'):
            deploy.finish_install(self.home, self.helper, self.app, self.support, service)
        self.assertEqual(deploy.upgrade_path(self.home).read_bytes(), original)
        self.assertEqual(service.starts, 0)

    def test_rollback_conflict_never_restarts_an_old_app_with_partially_restored_files(self):
        service, _ = self.prepare_upgrade()
        apply = deploy.Transaction.apply
        def conflicting_change(transaction):
            apply(transaction)
            self.config.write_bytes(b'{"external":"preserve"}')
            raise RuntimeError('fixture interrupted deployment')
        with patch.object(deploy.Transaction, 'apply', conflicting_change):
            with self.assertRaisesRegex(RuntimeError, '恢复未完成'):
                deploy.finish_install(self.home, self.helper, self.app, self.support, service)
        self.assertEqual(self.config.read_bytes(), b'{"external":"preserve"}')
        self.assertEqual(service.starts, 0)
        self.assertEqual(deploy.read_upgrade(self.home)['phase'], 'recovery-required')

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
        legacy_state = browser / 'cua-helper-config'
        legacy_state.mkdir()
        (legacy_state / 'browser-events.json').write_text('{"version": 1, "legacy": true}')
        (legacy_state / 'task-windows.json').write_text('{"version": 1, "legacy": true}')
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
                         {'KEEP_ME': 'value',
                          'QUIETBRIDGE_CONFIG_DIR': str(browser_layout(self.home)[3])})
        hooks = json.loads(settings.read_bytes())['hooks']['FinalStop']
        self.assertEqual(len(hooks), 1)
        self.assertIn('adapters/workbuddy/lifecycle.py', hooks[0]['hooks'][0]['command'])
        registration = self.home / 'Library/Application Support/Google/Chrome/NativeMessagingHosts/com.cua.browser_observer.json'
        self.assertEqual(json.loads(registration.read_bytes())['path'],
                         str(browser_layout(self.home)[1] / 'native-host-launcher.sh'))
        # The legacy browser directory is only a migration source: no second
        # native host is registered from it.
        self.assertFalse((browser / 'browser-observer-extension').exists())
        for name in ('browser-events.json', 'task-windows.json'):
            self.assertEqual((self.runtime / name).read_text(),
                             '{"version": 1, "legacy": true}')
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
        self.assertEqual(entry['env'], {'KEEP_ME': 'value',
                                          'QUIETBRIDGE_CONFIG_DIR': str(browser_layout(self.home)[3])})
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

    def test_deploy_renders_dsh_bundle_from_the_verified_release(self):
        outcome = self.install(FakeService())
        summary = outcome['dshBundle']
        self.assertTrue(summary['installed'], summary)
        self.assertEqual(summary['files'], 7)
        bundle = self.dsh_bundle(outcome)
        self.assertEqual(bundle.parent.parent, self.runtime / 'releases')
        self.assertEqual(json.loads((bundle / 'package.json').read_bytes())['name'], '@quietbridge/dsh')
        self.assertEqual({path.relative_to(bundle).as_posix() for path in bundle.rglob('*') if path.is_file()},
                         {'README.md', 'cordis.patch.yml', 'icon.svg', 'index.js', 'locale/en.json',
                          'locale/zh.json', 'package.json'})
        connector = bundle.parent / 'QuietBridge.app/Contents/Resources/bridge/server.py'
        self.assertTrue(connector.is_file())
        config = self.dsh_config(bundle)
        self.assertEqual(config['connector'], str(connector))
        self.assertEqual(config['helper'], str(self.helper))
        self.assertEqual(config['configDir'], str(browser_layout(self.home)[3]))
        self.assertEqual(config['serverName'], 'quietbridge')
        self.assertEqual(config['python'], self.dsh_python())
        self.assertTrue(Path(config['python']).is_absolute())
        self.assertEqual(summary['installCommand'], 'dsh plugin --profile <profile> add ' + str(bundle))
        journal = json.loads(Path(outcome['journal']).read_bytes())
        self.assertEqual(journal['phase'], 'committed')
        self.assertEqual(journal['dshBundle'], summary)
        tracked = {item['path'] for item in journal['files']}
        self.assertIn(str(bundle / 'cordis.patch.yml'), tracked)
        for path in bundle.rglob('*'):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)

    def test_repeated_deploy_keeps_the_rendered_dsh_bundle_idempotent(self):
        first = self.install(FakeService())
        bundle = self.dsh_bundle(first)
        before = {path.relative_to(bundle).as_posix(): path.read_bytes()
                  for path in bundle.rglob('*') if path.is_file()}
        second = self.install(FakeService())
        self.assertTrue(second['dshBundle']['installed'])
        self.assertEqual(second['dshBundle']['path'], first['dshBundle']['path'])
        after = {path.relative_to(bundle).as_posix(): path.read_bytes()
                 for path in bundle.rglob('*') if path.is_file()}
        self.assertEqual(after, before)
        journal = json.loads(Path(second['journal']).read_bytes())
        self.assertEqual(journal['phase'], 'committed')
        self.assertEqual(journal['dshBundle'], second['dshBundle'])

    def test_missing_dsh_template_reports_failure_without_blocking_install(self):
        shutil.rmtree(self.app / 'Contents/Resources/bridge/adapters/deepseek_harness/bundle')
        outcome = self.install(FakeService())
        self.assertTrue(outcome['installed'])
        summary = outcome['dshBundle']
        self.assertFalse(summary['installed'])
        self.assertTrue(summary['reason'])
        self.assertNotIn('path', summary)
        self.assertEqual(list((self.runtime / 'releases').glob('*/dsh-bundle')), [])
        journal = json.loads(Path(outcome['journal']).read_bytes())
        self.assertEqual(journal['phase'], 'committed')
        self.assertEqual(journal['dshBundle'], summary)
        entry = json.loads(self.config.read_bytes())['mcpServers']['quietbridge']
        self.assertEqual(entry['args'][-1], str(self.runtime / 'connector/connector.py'))
        self.assertTrue((self.extension / 'popup.html').is_file())

    def test_broken_dsh_package_json_reports_failure_without_blocking_install(self):
        (self.app / 'Contents/Resources/bridge/adapters/deepseek_harness/bundle/package.json'
         ).write_text('{not json')
        outcome = self.install(FakeService())
        self.assertTrue(outcome['installed'])
        self.assertFalse(outcome['dshBundle']['installed'])
        self.assertIn('package.json', outcome['dshBundle']['reason'])
        self.assertEqual(list((self.runtime / 'releases').glob('*/dsh-bundle')), [])

    def test_rollback_removes_new_dsh_bundle_files_with_the_transaction(self):
        original = self.config.read_bytes()
        apply = deploy.Transaction.apply
        def fail_after_apply(transaction):
            apply(transaction)
            raise RuntimeError('simulated dsh commit failure')
        with patch.object(deploy.Transaction, 'apply', fail_after_apply):
            with self.assertRaisesRegex(RuntimeError, 'simulated dsh commit failure'):
                self.install(FakeService())
        self.assertEqual(self.config.read_bytes(), original)
        bundles = list((self.runtime / 'releases').glob('*/dsh-bundle'))
        self.assertEqual(len(bundles), 1)
        self.assertEqual([path for path in bundles[0].rglob('*') if path.is_file()], [],
                         'the rolled-back derived bundle must not keep rendered files')
        journal = next((self.runtime / 'transactions').glob('*/journal.json'))
        self.assertEqual(json.loads(journal.read_bytes())['phase'], 'rolled-back')

    def test_reinstall_after_a_rolled_back_commit_renders_the_dsh_bundle_again(self):
        apply = deploy.Transaction.apply
        def fail_after_apply(transaction):
            apply(transaction)
            raise RuntimeError('simulated interrupted commit')
        with patch.object(deploy.Transaction, 'apply', fail_after_apply):
            with self.assertRaisesRegex(RuntimeError, 'simulated interrupted commit'):
                self.install(FakeService())
        residue = list((self.runtime / 'releases').glob('*/dsh-bundle'))
        self.assertEqual(len(residue), 1)
        self.assertFalse((residue[0] / 'package.json').exists())
        self.assertEqual([path for path in residue[0].rglob('*') if path.is_file()], [])
        outcome = self.install(FakeService())
        summary = outcome['dshBundle']
        self.assertTrue(summary['installed'], summary)
        self.assertEqual(summary['files'], 7)
        bundle = self.dsh_bundle(outcome)
        self.assertEqual(bundle, residue[0])
        self.assertTrue((bundle / 'package.json').is_file())
        self.assertEqual({path.relative_to(bundle).as_posix()
                          for path in bundle.rglob('*') if path.is_file()},
                         {'README.md', 'cordis.patch.yml', 'icon.svg', 'index.js', 'locale/en.json',
                          'locale/zh.json', 'package.json'})

    def dsh_bundle(self, outcome):
        summary = outcome['dshBundle']
        self.assertTrue(summary['installed'], summary)
        bundle = Path(summary['path'])
        self.assertEqual(bundle.name, 'dsh-bundle')
        self.assertTrue(bundle.is_dir())
        return bundle

    @staticmethod
    def dsh_python():
        return '/usr/bin/python3' if Path('/usr/bin/python3').is_file() else sys.executable

    def dsh_config(self, bundle):
        text = (bundle / 'cordis.patch.yml').read_text()
        self.assertNotIn('!!js process.env', text)
        match = re.search(r'^[ \t]*config:[ \t]*(\{.*\})[ \t]*$', text, re.M)
        self.assertIsNotNone(match, text)
        return json.loads(match.group(1))

    def test_new_release_digest_renders_a_new_bundle_and_keeps_the_old_release(self):
        first = self.install(FakeService())
        first_bundle = self.dsh_bundle(first)
        first_config = self.dsh_config(first_bundle)
        # Change packaged content so the content-addressed release digest changes.
        bridge = self.app / 'Contents/Resources/bridge/server.py'
        bridge.write_bytes(bridge.read_bytes() + b'\n# changed payload\n')
        second = self.install(FakeService())
        second_bundle = self.dsh_bundle(second)
        self.assertNotEqual(second_bundle, first_bundle)
        releases = sorted(path.name for path in (self.runtime / 'releases').iterdir()
                          if path.is_dir() and not path.name.startswith('.'))
        self.assertEqual(len(releases), 2, releases)
        self.assertTrue(first_bundle.is_dir(), 'the previous release keeps its rendered bundle')
        second_config = self.dsh_config(second_bundle)
        self.assertEqual(second_config['connector'],
                         str(second_bundle.parent / 'QuietBridge.app/Contents/Resources/bridge/server.py'))
        self.assertEqual(second_config['helper'], str(self.helper))
        self.assertEqual(second_config['configDir'], str(browser_layout(self.home)[3]))
        self.assertNotEqual(second_config['connector'], first_config['connector'])
        self.assertEqual(second['dshBundle']['files'], 7)

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
