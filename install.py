#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quietbridge user installer. Preview by default; --install applies changes.

加 --with-extension 可一并部署浏览器扩展的原生宿主并拉起本机安装页；
扩展本体仍需在 chrome://extensions 手动加载（Chrome 137 起无程序化入口）。

加 --dsh-bundle DIR 可生成可安装的 DeepSeek Harness bundle；它只写目标目录，
不修改 ~/.dsh profile，安装/启用仍走 DSH 的 install_bundle 流程。
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
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'services/mcp')]
from release_layout import bridge_sources
from compatibility import LEGACY_SERVER_NAMES, runtime_root
from adapters.workbuddy.guidance import CONNECTOR_DESCRIPTION


def checked_path(path):
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        if node.is_symlink():
            raise ValueError('拒绝符号链接路径: ' + str(node))
    return path


def sources(root):
    app = root / 'apps/macos' / 'QuietBridge.app'
    mapping = bridge_sources(root)
    files = list(mapping.values())
    binary = app / 'Contents/MacOS/quietbridge-native'
    if not all(p.is_file() for p in files + [binary, app / 'Contents/Info.plist']):
        raise ValueError('缺少组件；请保持 install.py、services/mcp 和 apps/macos 同级')
    # Reject source links too; no traversal into arbitrary user locations.
    for base in (app, root / 'services/mcp'):
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
            if path in (source / 'services/mcp/server.py', source / 'cua-mcp-bridge/server.py'):
                return True
            if path.name == 'server.py' and runtime in path.parents:
                return True
    return False


def extension_step(root, home, apply=False, launch=False):
    """部署浏览器扩展的原生消息宿主，可选再拉起安装页。

    扩展本体无法程序化装载：Chrome 从 137 起移除了 --load-extension。
    所以这里只自动化宿主这一半，「加载已解压」和点授权仍归本人。
    """
    directory = root / 'extensions/chromium'
    if not (directory / 'manifest.json').is_file():
        raise ValueError('缺少浏览器扩展目录：' + str(directory))
    # 必须早于 import：模块内再禁用已来不及阻止本模块的缓存写入。
    # Chrome 不允许扩展目录中出现 __pycache__。
    sys.dont_write_bytecode = True
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
    runtime = checked_path(runtime_root(home))
    releases = runtime / 'releases'
    destination = releases / digest
    config = checked_path(home / '.workbuddy/mcp.json')
    data, original = read_config(config)
    servers = data.setdefault('mcpServers', {})
    key = 'quietbridge'
    # Preserve an existing legacy connection key when it is demonstrably ours.
    if key in servers and not owned(servers[key], root, runtime):
        raise ValueError('quietbridge 配置键已由其他组件占用，未覆盖')
    if key not in servers:
        key = next((alias for alias in LEGACY_SERVER_NAMES if alias in servers and owned(servers[alias], root, runtime)), key)
    if verify_signature:
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(app)], check=True, capture_output=True)
    helper = destination / 'QuietBridge.app/Contents/MacOS/quietbridge-native'
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
                        '=anchor apple generic and identifier "com.quietbridge.helper" and certificate leaf[subject.OU] = "62RNP54C47"',
                        str(public_helper)], check=True, capture_output=True)
        helper = public_helper / 'Contents/MacOS/quietbridge-native'
    old = servers.get(key, {})
    entry = dict(old)
    env = dict(old.get('env', {}))
    env.pop('CUA_HELPER_PATH', None)
    env['QUIETBRIDGE_HELPER_PATH'] = str(helper)
    entry.update(command=sys.executable, args=['-B', str(destination / 'bridge/server.py')], env=env)
    if 'description' not in entry:
        entry['description'] = CONNECTOR_DESCRIPTION
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
        shutil.copytree(app, stage / 'QuietBridge.app')
        (stage / 'bridge').mkdir()
        for path in files:
            target = stage / 'bridge' / next(relative for relative, source in bridge_sources(root).items() if source == path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        (stage / 'install-receipt.json').write_text(json.dumps({'digest': digest}), encoding='utf-8')
        stage.rename(destination)
    else:
        receipt = destination / 'install-receipt.json'
        if not receipt.is_file() or json.loads(receipt.read_text()).get('digest') != digest:
            raise ValueError('现有版本目录无法核验；未修改配置')
        pairs = [(path, destination / 'bridge' / next(relative for relative, source in bridge_sources(root).items() if source == path)) for path in files] + [
                 (app / 'Contents/MacOS/quietbridge-native', helper),
                 (app / 'Contents/Info.plist', destination / 'QuietBridge.app/Contents/Info.plist')]
        if any(not b.is_file() or a.read_bytes() != b.read_bytes() for a, b in pairs):
            raise ValueError('现有版本内容与源文件不一致；未修改配置')
    # Verify the deployed MCP handshake without launching helper or interacting with UI.
    request = json.dumps({'id': 1, 'method': 'initialize', 'params': {}}) + '\n'
    reply = subprocess.run([sys.executable, '-B', str(destination / 'bridge/server.py')],
                           input=request, capture_output=True, text=True, timeout=15,
                           env=dict(os.environ, QUIETBRIDGE_HELPER_PATH=str(helper)))
    if reply.returncode != 0 or json.loads(reply.stdout)['result']['serverInfo']['name'] != 'quietbridge':
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


def dsh_helper_path(root, helper=None, public_helper=None):
    """Resolve the helper executable embedded in a rendered DSH bundle."""
    if helper is not None:
        return checked_path(helper)
    if public_helper is not None:
        return checked_path(public_helper) / 'Contents/MacOS/quietbridge-native'
    for candidate in (root / 'apps/macos/QuietBridge.app/Contents/MacOS/quietbridge-native',
                      Path('/Applications/QuietBridge.app/Contents/MacOS/quietbridge-native')):
        if candidate.is_file():
            return checked_path(candidate)
    raise ValueError('缺少 helper 可执行文件；请用 --dsh-helper 指定')


def dsh_bundle(root, home, destination, *, apply=False, connector=None, helper=None,
               public_helper=None, server_name='quietbridge'):
    """Render an installable DeepSeek Harness bundle; never writes the DSH profile.

    The rendered directory is the input for the host's own ``install_bundle``
    flow. This installer does not edit ``~/.dsh``: installing, enabling and
    trusting a bundle stay explicit user actions.
    """
    from adapters.deepseek_harness.configuration import bundle_render
    root, home = checked_path(root), checked_path(home)
    source = checked_path(root / 'adapters/deepseek_harness/bundle')
    if not (source / 'package.json').is_file():
        raise ValueError('缺少 DSH bundle 模板：' + str(source))
    bridge = checked_path(connector or root / 'services/mcp/server.py')
    if not bridge.is_file():
        raise ValueError('缺少 DSH connector 入口：' + str(bridge))
    resolved_helper = dsh_helper_path(root, helper, public_helper)
    target = Path(destination).expanduser()
    if not target.is_absolute():
        raise ValueError('--dsh-bundle 必须是绝对路径')
    # The destination is a new output directory chosen by the caller; only the
    # setup inputs and the helper stay under the strict symlink-free resolver.
    plan = bundle_render(source, target, python=sys.executable,
                         connector=bridge, helper=resolved_helper,
                         config_dir=checked_path(runtime_root(home)), server_name=server_name,
                         apply=apply)
    plan['mode'] = 'install' if apply else 'preview'
    plan['profileChanged'] = False
    plan['next'] = ('安装该目录：dsh plugin --profile <profile> add <目录>（官方 CLI，'
                    '会写入 link 依赖与 dsh.profile.bundles）或用宿主 plugin_manager '
                    'install_bundle；本脚本不修改 ~/.dsh。')
    plan['limitations'] = ['未写入或启用 DSH profile，安装与启用仍需用户显式操作',
                           'DSH 由签名 helper 承担的 TCC 归属尚未核实',
                           '本轮不宣称 DSH 预览可用；原生宿主窗口绑定是后续工作']
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='实际部署；默认只预览')
    parser.add_argument('--with-extension', action='store_true',
                        help='同时部署浏览器扩展的原生宿主，并拉起本机安装页')
    parser.add_argument('--public-helper', type=Path, help='使用已发布并验签的系统可读 helper .app')
    parser.add_argument('--no-open', action='store_true', help='配合 --with-extension：不自动打开安装页')
    parser.add_argument('--dsh-bundle', type=Path, metavar='DIR',
                        help='生成可安装的 DeepSeek Harness bundle 到 DIR（配合 --install 才写盘）')
    parser.add_argument('--dsh-connector', type=Path, help='DSH bundle 使用的 connector 入口')
    parser.add_argument('--dsh-helper', type=Path, help='DSH bundle 使用的 helper 可执行文件')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('仅支持 macOS')
    try:
        if args.dsh_bundle is not None:
            result = dsh_bundle(ROOT, Path.home(), args.dsh_bundle, apply=args.install,
                                connector=args.dsh_connector, helper=args.dsh_helper,
                                public_helper=args.public_helper)
        else:
            result = install(ROOT, Path.home(), args.install,
                             with_extension=args.with_extension,
                             launch_installer=not args.no_open,
                             public_helper=args.public_helper)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        print('安装未完成: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
