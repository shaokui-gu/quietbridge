#!/usr/bin/env python3
"""Publish signed helper code for system authorization-host signature inspection.

Does not change authorization rules, start the helper, or edit user configuration.
The base helper remains installed when the separate Authorization Plug-in is removed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

PUBLIC_ROOT = Path('/Library/Application Support/WorkBuddyCUAHelper')
REQUIREMENT = '=anchor apple generic and identifier "com.quietbridge.helper" and certificate leaf[subject.OU] = "62RNP54C47"'


def checked(path):
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        if node.is_symlink():
            raise ValueError('拒绝符号链接: ' + str(node))
    return path


def verify(app):
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', '-R', REQUIREMENT, str(app)], check=True)


def publish(source):
    if os.geteuid() != 0:
        raise PermissionError('发布系统可读 helper 需要管理员权限')
    source = checked(source)
    verify(source)
    binary = source / 'Contents/MacOS/quietbridge-native'
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    releases = PUBLIC_ROOT / 'releases'
    checked(releases)
    for node in list(reversed(PUBLIC_ROOT.parents)) + [PUBLIC_ROOT, releases]:
        if not node.exists():
            node.mkdir(mode=0o755)
        info = node.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('系统程序目录归属或权限不安全: ' + str(node))
    destination = releases / digest[:16]
    app = destination / 'QuietBridge.app'
    checked(destination)
    if destination.exists():
        verify(app)
        if (app / 'Contents/MacOS/quietbridge-native').read_bytes() != binary.read_bytes():
            raise ValueError('目标版本内容不一致')
    else:
        staging = Path(tempfile.mkdtemp(prefix='.publish-', dir=releases))
        try:
            staged_app = staging / 'QuietBridge.app'
            shutil.copytree(source, staged_app, symlinks=False)
            subprocess.run(['/usr/sbin/chown', '-R', 'root:wheel', str(staging)], check=True)
            subprocess.run(['/bin/chmod', '-R', 'a+rX,go-w', str(staging)], check=True)
            os.chmod(staging, 0o755)
            verify(staged_app)
            (staging / 'receipt.json').write_text(json.dumps({'sha256': digest, 'purpose': 'signed helper code only'}))
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    return {'published': True, 'app': str(app), 'helper': str(app / 'Contents/MacOS/quietbridge-native'),
            'version': info['CFBundleShortVersionString'], 'build': info['CFBundleVersion'],
            'authorizationPolicyChanged': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(publish(args.bundle), indent=2))
