#!/usr/bin/env python3
"""workbuddy-cua user installer. Preview by default; --install applies changes.

加 --with-extension 可一并部署浏览器扩展的原生宿主并拉起本机安装页；
扩展本体仍需在 chrome://extensions 手动加载（Chrome 137 起无程序化入口）。
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def checked_path(path):
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        if node.is_symlink():
            raise ValueError('拒绝符号链接路径: ' + str(node))
    return path


def sources(root):
    app = root / 'cua-helper-build' / 'workbuddy-cua.app'
    files = [root / 'cua-mcp-bridge' / name for name in ('server.py', 'browser_events.py')]
    binary = app / 'Contents/MacOS/workbuddy-cua-native'
    if not all(p.is_file() for p in files + [binary, app / 'Contents/Info.plist']):
        raise ValueError('缺少组件；请保持 install.py、cua-mcp-bridge 和 cua-helper-build 同级')
    # Reject source links too; no traversal into arbitrary user locations.
    for base in (app, root / 'cua-mcp-bridge'):
        checked_path(base)
    for directory, dirs, names in os.walk(app, followlinks=False):
        for name in dirs + names:
            if (Path(directory) / name).is_symlink():
                raise ValueError('应用包包含符号链接，当前安装器不支持')
    for path in files:
        checked_path(path)
    plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    digest = hashlib.sha256()
    for path in files + [binary, app / 'Contents/Info.plist']:
        digest.update(path.read_bytes())
    return app, files, digest.hexdigest()[:16]


def read_config(path):
    checked_path(path)
    if not path.exists():
        return {}, None
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
        raise ValueError('mcp.json 必须是对象且 mcpServers 必须是对象；未写入')
    return data, raw


def owned(entry, source, runtime):
    if not isinstance(entry, dict):
        return False
    args = entry.get('args', [])
    if not isinstance(args, list):
        return False
    for arg in args:
        if isinstance(arg, str):
            path = Path(arg)
            if path == source / 'cua-mcp-bridge/server.py':
                return True
            if path.name == 'server.py' and runtime in path.parents:
                return True
    return False


def extension_step(root, home, apply=False, launch=False):
    """部署浏览器扩展的原生消息宿主，可选再拉起安装页。

    扩展本体无法程序化装载：Chrome 从 137 起移除了 --load-extension。
    所以这里只自动化宿主这一半，「加载已解压」和点授权仍归本人。
    """
    directory = root / 'browser-observer-extension'
    if not (directory / 'manifest.json').is_file():
        raise ValueError('缺少浏览器扩展目录：' + str(directory))
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    import install_native_host as host
    planned = host.plan(home=home)
    summary = {'extensionId': planned['extensionId'], 'manifestPath': planned['manifestPath']}
    if not apply:
        summary['mode'] = 'preview'
        return summary
    outcome = host.install(planned, update=True, home=home)
    summary.update(mode=outcome['mode'], changed=outcome['changed'])
    if launch:
        command = directory / 'installer' / '安装扩展.command'
        if not command.is_file():
            raise ValueError('缺少安装页启动器：' + str(command))
        # 交给终端执行：URL 打印在那里，页面由用户自己关。
        subprocess.run(['/usr/bin/open', str(command)], check=False)
        summary['installerLaunched'] = True
    return summary


def install(root, home, apply=False, verify_signature=True, with_extension=False, launch_installer=True, public_helper=None):
    if sys.version_info < (3, 9):
        raise ValueError('需要 Python 3.9 或以上；本版不会自动下载解释器')
    root, home = checked_path(root), checked_path(home)
    app, files, digest = sources(root)
    runtime = checked_path(home / 'Library/Application Support/workbuddy-cua')
    releases = runtime / 'releases'
    destination = releases / digest
    config = checked_path(home / '.workbuddy/mcp.json')
    data, original = read_config(config)
    servers = data.setdefault('mcpServers', {})
    key = 'workbuddy-cua'
    # Preserve an existing legacy connection key when it is demonstrably ours.
    if key in servers and not owned(servers[key], root, runtime):
        raise ValueError('workbuddy-cua 配置键已由其他组件占用，未覆盖')
    if key not in servers and 'cua' in servers and owned(servers['cua'], root, runtime):
        key = 'cua'
    if verify_signature:
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(app)], check=True, capture_output=True)
    helper = destination / 'workbuddy-cua.app/Contents/MacOS/workbuddy-cua-native'
    if public_helper is not None:
        public_helper = checked_path(public_helper)
        public_root = Path('/Library/Application Support/WorkBuddyCUAHelper/releases')
        if not public_helper.is_relative_to(public_root):
            raise ValueError('公开 helper 必须位于 root 管理的 WorkBuddyCUAHelper 版本目录')
        for node in (public_helper, *public_helper.parents):
            info = node.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('公开 helper 目录归属或权限不安全: ' + str(node))
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', '-R',
                        '=anchor apple generic and identifier "com.workbuddy.cua.helper" and certificate leaf[subject.OU] = "62RNP54C47"',
                        str(public_helper)], check=True, capture_output=True)
        helper = public_helper / 'Contents/MacOS/workbuddy-cua-native'
    old = servers.get(key, {})
    entry = dict(old)
    env = dict(old.get('env', {}))
    env['CUA_HELPER_PATH'] = str(helper)
    entry.update(command=sys.executable, args=['-B', str(destination / 'bridge/server.py')],
                 env=env, description='workbuddy-cua：macOS 原生界面观察与操作（AX/坐标/键盘），网页内容走其浏览器扩展观察通道；需已授权辅助功能与屏幕录制')
    servers[key] = entry
    rendered = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    plan = {'mode': 'install' if apply else 'preview', 'release': str(destination),
            'config': str(config), 'connectionKey': key, 'python': sys.executable,
            'configChanged': original != rendered,
            'limitations': ['系统权限与连接信任需本人确认', '需Python3.9+，不内置运行环境',
                            '浏览器扩展本体仍需在 chrome://extensions 手动加载并授权'
                            if with_extension else '本版只安装桌面组件；技能和浏览器扩展不自动安装',
                            '未自动注册登录启动，未自动启动helper或执行界面操作']}
    if with_extension:
        plan['extension'] = extension_step(root, home)
    if not apply:
        return plan
    # Versioned releases are immutable; no in-place replacement of running apps.
    checked_path(destination)
    if not destination.exists():
        releases.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = Path(tempfile.mkdtemp(prefix='.install-', dir=releases))
        shutil.copytree(app, stage / 'workbuddy-cua.app')
        (stage / 'bridge').mkdir()
        for path in files:
            shutil.copy2(path, stage / 'bridge' / path.name)
        (stage / 'install-receipt.json').write_text(json.dumps({'digest': digest}), encoding='utf-8')
        stage.rename(destination)
    else:
        receipt = destination / 'install-receipt.json'
        if not receipt.is_file() or json.loads(receipt.read_text()).get('digest') != digest:
            raise ValueError('现有版本目录无法核验；未修改配置')
        pairs = [(files[0], destination / 'bridge/server.py'), (files[1], destination / 'bridge/browser_events.py'),
                 (app / 'Contents/MacOS/workbuddy-cua-native', helper),
                 (app / 'Contents/Info.plist', destination / 'workbuddy-cua.app/Contents/Info.plist')]
        if any(not b.is_file() or a.read_bytes() != b.read_bytes() for a, b in pairs):
            raise ValueError('现有版本内容与源文件不一致；未修改配置')
    # Verify the deployed MCP handshake without launching helper or interacting with UI.
    request = json.dumps({'id': 1, 'method': 'initialize', 'params': {}}) + '\n'
    reply = subprocess.run([sys.executable, '-B', str(destination / 'bridge/server.py')],
                           input=request, capture_output=True, text=True, timeout=15,
                           env=dict(os.environ, CUA_HELPER_PATH=str(helper)))
    if reply.returncode != 0 or json.loads(reply.stdout)['result']['serverInfo']['name'] != 'workbuddy-cua':
        raise ValueError('安装副本握手失败；配置未修改，保留版本用于诊断')
    if original != rendered:
        config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Reject changes made by another process between planning and commit.
        current = config.read_bytes() if config.exists() else None
        if current != original:
            raise ValueError('配置被其他程序修改，请重试；未覆盖')
        if original is not None:
            backup_dir = runtime / 'backups'
            backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = datetime.datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')
            backup = backup_dir / ('mcp-' + stamp + '.json')
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(original)
            plan['backup'] = str(backup)
        fd, temporary = tempfile.mkstemp(prefix='.mcp-install-', dir=config.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config)
    plan['handshakeVerified'] = True
    plan['next'] = '在 WorkBuddy 连接器管理的自定义连接器入口确认信任/启用；按需要授予辅助功能与屏幕录制。写配置不等于已启用。'
    if with_extension:
        plan['extension'] = extension_step(root, home, apply=True, launch=launch_installer)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='实际部署；默认只预览')
    parser.add_argument('--with-extension', action='store_true',
                        help='同时部署浏览器扩展的原生宿主，并拉起本机安装页')
    parser.add_argument('--public-helper', type=Path, help='使用已发布并验签的系统可读 helper .app')
    parser.add_argument('--no-open', action='store_true', help='配合 --with-extension：不自动打开安装页')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('仅支持 macOS')
    try:
        print(json.dumps(install(ROOT, Path.home(), args.install,
                                 with_extension=args.with_extension,
                                 launch_installer=not args.no_open,
                                 public_helper=args.public_helper), ensure_ascii=False, indent=2))
    except Exception as error:
        print('安装未完成: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
