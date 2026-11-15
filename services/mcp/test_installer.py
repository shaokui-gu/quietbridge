"""Isolated installer tests; never write real user configuration or launch GUI."""
import importlib.util
import json
from pathlib import Path
import tempfile
import plistlib
import shutil
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('quietbridge_installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.source = self.base / 'source'
        for relative, source in installer.bridge_sources(ROOT).items():
            target = self.source / ('adapters' if relative.parts[0] == 'adapters' else 'services/mcp')
            target = self.source / relative if relative.parts[0] == 'adapters' else target / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        app = self.source / 'apps/macos/QuietBridge.app/Contents'
        (app / 'MacOS').mkdir(parents=True)
        (app / 'MacOS/quietbridge-native').write_bytes(b'test-only-helper-not-launched')
        (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleName': 'QuietBridge'}))

    def run_install(self, apply=True):
        return installer.install(self.source, self.home, apply=apply, verify_signature=False)

    def test_preview_writes_nothing(self):
        self.run_install(False)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_real_deployment_handshake_idempotent_and_preserves_other_servers(self):
        config = self.home / '.workbuddy/mcp.json'
        config.parent.mkdir()
        old = {'mcpServers': {'unrelated': {'command': 'keep'}}, 'otherSetting': 7}
        config.write_text(json.dumps(old))
        first = self.run_install()
        self.assertTrue(first['handshakeVerified'])
        self.assertEqual(json.loads(config.read_text())['mcpServers']['unrelated'], old['mcpServers']['unrelated'])
        self.assertEqual(Path(first['backup']).read_bytes(), json.dumps(old).encode())
        before = config.read_bytes()
        second = self.run_install()
        self.assertFalse(second['configChanged'])
        self.assertEqual(config.read_bytes(), before)
        self.assertNotIn('backup', second)

    def test_invalid_json_not_overwritten(self):
        config = self.home / '.workbuddy/mcp.json'
        config.parent.mkdir()
        config.write_text('invalid')
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertEqual(config.read_text(), 'invalid')

    def test_foreign_key_not_overwritten(self):
        config = self.home / '.workbuddy/mcp.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'mcpServers': {'quietbridge': {'command': 'foreign'}}}))
        with self.assertRaises(ValueError):
            self.run_install()

    def test_symlink_config_rejected(self):
        config = self.home / '.workbuddy/mcp.json'
        config.parent.mkdir()
        target = self.home / 'other.json'
        target.write_text('{}')
        config.symlink_to(target)
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertEqual(target.read_text(), '{}')

    def test_failed_handshake_leaves_config_unchanged(self):
        with patch.object(installer.subprocess, 'run', side_effect=RuntimeError('synthetic handshake failure')):
            with self.assertRaises(RuntimeError):
                self.run_install()
        self.assertFalse((self.home / '.workbuddy/mcp.json').exists())

    def render_dsh(self, destination, apply=False, helper=None):
        helper = helper or self.base / 'helper'
        helper.write_bytes(b'test-only-helper')
        return installer.dsh_bundle(installer.ROOT, self.home, destination,
                                    apply=apply, helper=helper)

    def test_dsh_bundle_preview_writes_nothing(self):
        destination = self.base / 'rendered'
        plan = self.render_dsh(destination)
        self.assertEqual(plan['mode'], 'preview')
        self.assertFalse(destination.exists())
        self.assertFalse((self.home / '.dsh').exists())
        self.assertFalse(plan['profileChanged'])
        self.assertIn('@quietbridge/dsh', plan['patch'])
        self.assertIn('id: quietbridge-dsh', plan['patch'])
        self.assertNotIn('!!js process.env', plan['patch'])
        self.assertFalse((self.home / '.workbuddy').exists())

    def test_dsh_bundle_applied_is_installable_and_idempotent(self):
        destination = self.base / 'rendered'
        plan = self.render_dsh(destination, apply=True)
        self.assertTrue(plan['applied'])
        manifest = json.loads((destination / 'package.json').read_text())
        self.assertEqual(manifest['name'], '@quietbridge/dsh')
        self.assertIn('patch', manifest['dsh']['bundle'])
        self.assertEqual(manifest['exports']['./icon'], './icon.svg')
        patch = (destination / 'cordis.patch.yml').read_text()
        self.assertIn('config: {"python"', patch)
        self.assertIn(str(self.base / 'helper'), patch)
        self.assertNotIn('!!js process.env', patch)
        # Template tests and caches must not become part of the installed bundle.
        self.assertFalse((destination / 'test').exists())
        before = {path.name for path in destination.iterdir()}
        again = self.render_dsh(destination, apply=True)
        self.assertTrue(again['applied'])
        self.assertEqual({path.name for path in destination.iterdir()}, before)
        self.assertFalse((self.home / '.dsh').exists())

    def test_dsh_bundle_refuses_foreign_destination(self):
        destination = self.base / 'foreign'
        destination.mkdir()
        (destination / 'package.json').write_text(json.dumps({'name': 'other'}))
        with self.assertRaises(ValueError):
            self.render_dsh(destination, apply=True)
        self.assertEqual(json.loads((destination / 'package.json').read_text())['name'], 'other')


if __name__ == '__main__':
    unittest.main()
