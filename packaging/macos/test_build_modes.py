"""Packaging mode checks: installed payload must stay readable by the desktop user.

A restrictive umask once produced 0600 files under the DeepSeek Harness adapter;
PackageKit preserves source modes, so the installed app contained root:wheel
0600 files. The postinstall deploy runs as the desktop user, and its
``codesign --verify`` then failed with "Permission denied", aborting the install.
These tests keep payload normalization and the bridge source modes honest.
"""
import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load_build():
    spec = importlib.util.spec_from_file_location('quietbridge_build', ROOT / 'packaging/macos/build.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_layout():
    spec = importlib.util.spec_from_file_location('quietbridge_release_layout', ROOT / 'tools/release_layout.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PayloadModeTests(unittest.TestCase):
    def setUp(self):
        self.build = load_build()

    def test_normalize_tree_modes_makes_files_readable_and_keeps_executables(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'nested').mkdir()
            (root / 'nested').chmod(0o700)
            plain = root / 'nested' / 'bridge.py'
            plain.write_bytes(b'print(1)\n')
            plain.chmod(0o600)
            executable = root / 'nested' / 'quietbridge-native'
            executable.write_bytes(b'binary')
            executable.chmod(0o700)
            self.build.normalize_tree_modes(root)
            self.assertEqual(stat.S_IMODE((root / 'nested').stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(plain.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o755)

    def test_normalize_tree_modes_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'real').write_bytes(b'x')
            (root / 'link').symlink_to(root / 'real')
            with self.assertRaises(SystemExit):
                self.build.normalize_tree_modes(root)

    def test_bridge_sources_are_world_readable(self):
        sources = load_layout().bridge_sources(ROOT)
        unreadable = []
        for relative, source in sources.items():
            mode = stat.S_IMODE(source.stat().st_mode)
            if not mode & 0o444:
                unreadable.append('%s (%o)' % (relative, mode))
            if mode & 0o111:
                self.assertTrue(mode & 0o111, relative)
        self.assertEqual(unreadable, [], 'bridge 源文件不可读: ' + ', '.join(unreadable))


if __name__ == '__main__':
    unittest.main()
