"""Executed Python entry scripts must declare UTF-8 for non-ASCII sources.

Python 3.9's file tokenizer can misread a multi-byte character that straddles its
8192-byte read buffer when the script has no coding cookie, even though the file
is valid UTF-8 (observed on the large ``services/mcp/server.py`` under the macOS
system Python 3.9). Imported modules are compiled from bytes and are unaffected;
scripts run as ``python file.py`` are not. Keep an explicit PEP 263 cookie on
every executable entry point so direct execution stays portable.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
COOKIE = '# -*- coding: utf-8 -*-'
SKIP_PARTS = {'.git', '.build', '.build-legacy', 'dist', 'output', 'node_modules',
              '__pycache__', '.caches'}


def entry_scripts():
    discovered = []
    for path in sorted(ROOT.rglob('*.py')):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        raw = path.read_bytes()
        if all(byte < 128 for byte in raw):
            continue
        first = raw.split(b'\n', 1)[0]
        # Shebang scripts are executed directly; adapter lifecycle modules are
        # run by host hooks/CLI even without a shebang.
        if first.startswith(b'#!') or path.name == 'lifecycle.py':
            discovered.append((path, raw))
    return discovered


class EntryEncodingTests(unittest.TestCase):
    def test_executed_non_ascii_entry_scripts_declare_utf8(self):
        missing = []
        for path, raw in entry_scripts():
            head = b'\n'.join(raw.split(b'\n', 3)[:2]).decode('utf-8')
            if COOKIE not in head:
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(missing, [], '缺少 UTF-8 编码声明: ' + ', '.join(missing))

    def test_server_entry_is_covered(self):
        names = {str(path.relative_to(ROOT)) for path, _ in entry_scripts()}
        self.assertIn('services/mcp/server.py', names)


if __name__ == '__main__':
    unittest.main()
