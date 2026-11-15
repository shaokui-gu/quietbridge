#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage the explicit experimental installation outside protected Documents.

Run as the desktop user. sudo handles administrator authentication locally;
passwords never pass through this script.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from install import require_supported_lock_ui

ROOT = Path(__file__).resolve().parent


def main():
    # Fail before publishing a helper, staging files or asking for root access.
    require_supported_lock_ui()
    if not (ROOT / 'build/WorkBuddyCUAAuthorizationPlugin.bundle').is_dir():
        raise RuntimeError('请先构建授权插件')
    with tempfile.TemporaryDirectory(prefix='quietbridge-install-', dir='/private/tmp') as directory:
        stage = Path(directory)
        for filename in ('install.py', 'publish_helper.py'):
            shutil.copy2(ROOT / filename, stage / filename)
        (stage / 'build').mkdir()
        shutil.copytree(ROOT / 'build/WorkBuddyCUAAuthorizationPlugin.bundle',
                        stage / 'build/WorkBuddyCUAAuthorizationPlugin.bundle')
        shutil.copytree(ROOT.parents[1] / 'apps/macos/QuietBridge.app', stage / 'helper.app')
        (stage / 'apply.py').write_text('''import json
from pathlib import Path
import install
import publish_helper
published = publish_helper.publish(Path(__file__).parent / "helper.app")
result = install.install(experimental=True)
print(json.dumps({"published": published, "authorization": result}))
''')
        # stderr/stdin stay connected to the terminal for sudo's own prompt.
        result = subprocess.run(['sudo', sys.executable, '-B', str(stage / 'apply.py')],
                                check=True, stdout=subprocess.PIPE, text=True)
        values = json.loads(result.stdout)
        print(json.dumps(values, ensure_ascii=False, indent=2))
        subprocess.run([sys.executable, str(ROOT.parents[1] / 'install.py'), '--install',
                        '--public-helper', values['published']['app']], check=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('实验安装未完成: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
