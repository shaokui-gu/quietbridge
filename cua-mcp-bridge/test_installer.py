"""Isolated installer tests; never write real user configuration or launch GUI."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cua_installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()

    def run_install(self, apply=True):
        return installer.install(ROOT, self.home, apply=apply, verify_signature=False)

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
        config.write_text(json.dumps({'mcpServers': {'workbuddy-cua': {'command': 'foreign'}}}))
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


if __name__ == '__main__':
    unittest.main()
