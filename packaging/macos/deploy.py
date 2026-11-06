#!/usr/bin/env python3
"""Deploy user components transactionally. Never change authorizationdb.

Run as the desktop user, after the root installer has published the signed helper.
The journal covers user files and the extension directory, not the PKG payload.
"""
import argparse
import ctypes
import datetime
import fcntl
import fnmatch
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

LABEL = 'com.workbuddy.cua.menubar'
APP = Path('/Applications/workbuddy-cua.app')
SUPPORT = Path('/Library/Application Support/workbuddy-cua')
PUBLIC = Path('/Library/Application Support/WorkBuddyCUAHelper/releases')
TEAM = '62RNP54C47'
EXTENSION_IGNORES = ('native-host-config.json', 'native-host-launcher.sh', '__pycache__',
                     '.DS_Store', '._*', '*.test.mjs')


def checked(path):
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        if node.is_symlink():
            raise ValueError('拒绝符号链接: ' + str(node))
    return path


def read(path):
    path = checked(path)
    if not path.exists():
        return None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('配置必须是本人普通文件: ' + str(path))
    return path.read_bytes()


def atomic(path, data, mode=0o600):
    path = checked(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.cua-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def tree_digest(directory, ignore=()):
    checked(directory)
    digest = hashlib.sha256()
    for path in sorted(directory.rglob('*')):
        if any(fnmatch.fnmatchcase(part, pattern) for part in path.relative_to(directory).parts for pattern in ignore):
            continue
        checked(path)
        if path.is_file():
            digest.update(str(path.relative_to(directory)).encode() + b'\0')
            digest.update(path.read_bytes())
        elif not path.is_dir():
            raise ValueError('组件包含非普通文件: ' + str(path))
    return digest.hexdigest()


def owned(entry, home):
    if not isinstance(entry, dict) or not isinstance(entry.get('args'), list):
        return False
    runtime = home / 'Library/Application Support/workbuddy-cua/releases'
    for arg in entry['args']:
        if not isinstance(arg, str):
            continue
        path = Path(arg)
        if (path == APP / 'Contents/Resources/bridge/server.py' or
                (path.name == 'server.py' and runtime in path.parents)):
            return True
        # Recognise source installs created by install.py without baking one
        # developer's checkout location into the distributed installer.
        if (path.name == 'server.py' and path.parent.name == 'cua-mcp-bridge' and home in path.parents
                and isinstance(entry.get('description'), str) and entry['description'].startswith('workbuddy-cua：')):
            source = read(path)
            if source is not None and b'SERVER_NAME = "workbuddy-cua"' in source:
                return True
    return False


def config_plan(home, bridge, helper):
    config = checked(home / '.workbuddy/mcp.json')
    original = read(config)
    data = json.loads(original) if original is not None else {}
    if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
        raise ValueError('mcp.json 格式无法识别；未修改')
    servers = data.setdefault('mcpServers', {})
    key = 'workbuddy-cua'
    if key in servers and not owned(servers[key], home):
        raise ValueError('workbuddy-cua 配置键已被其他组件占用；未覆盖')
    if key not in servers and 'cua' in servers and owned(servers['cua'], home):
        key = 'cua'
    entry = dict(servers.get(key, {}))
    if not isinstance(entry.get('env', {}), dict):
        raise ValueError('现有连接 env 格式错误；未修改')
    env = dict(entry.get('env', {}))
    env['CUA_HELPER_PATH'] = str(helper)
    entry.update(command=sys.executable, args=['-B', str(bridge)], env=env)
    servers[key] = entry
    return config, original, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()


def preflight(home):
    if sys.version_info < (3, 9):
        raise ValueError('需要 Python 3.9 或更高版本')
    if sys.platform != 'darwin' or int(platform.mac_ver()[0].split('.')[0]) < 14 or platform.machine() != 'arm64':
        raise ValueError('此安装包需要 Apple Silicon 和 macOS 14 或更高版本；请使用原生 arm64 Python')
    home = checked(home)
    if home.stat().st_uid != os.getuid() or os.geteuid() == 0:
        raise ValueError('用户组件必须以桌面用户身份安装')
    config_plan(home, APP / 'Contents/Resources/bridge/server.py', APP)
    runtime = checked(home / 'Library/Application Support/workbuddy-cua')
    for relative in ('extension/extension-stage', 'releases', 'transactions'):
        checked(runtime / relative)
    agent = checked(home / 'Library/LaunchAgents' / (LABEL + '.plist'))
    previous_agent = read(agent)
    if previous_agent is not None:
        value = plistlib.loads(previous_agent)
        args = value.get('ProgramArguments', [])
        if (value.get('Label') != LABEL or not isinstance(args, list) or not args or
                not (Path(args[0]) == APP / 'Contents/MacOS/workbuddy-cua-native' or
                     runtime / 'releases' in Path(args[0]).parents)):
            raise ValueError('登录启动项被其他组件占用；未覆盖')
    for journal in sorted((runtime / 'transactions').glob('*/journal.json')):
        if json.loads(read(journal))['phase'] not in ('committed', 'rolled-back'):
            raise ValueError('发现未完成安装；请先运行“恢复安装.command”：' + str(journal))
    return runtime


class Transaction:
    def __init__(self, directory, files, extension, source):
        self.directory = directory
        self.journal = directory / 'journal.json'
        self.values = {'phase': 'prepared', 'files': [], 'extension': str(extension),
                       'extensionDigest': tree_digest(source),
                       'hadExtension': extension.exists()}
        shutil.copytree(source, directory / 'extension-next')
        for index, (path, original, data, mode) in enumerate(files):
            if original == data:
                continue
            if original is not None:
                atomic(directory / ('backup-' + str(index)), original)
            self.values['files'].append({'path': str(path), 'backup': 'backup-' + str(index),
                                        'hadFile': original is not None,
                                        'oldMode': stat.S_IMODE(path.stat().st_mode) if original is not None else 0o600,
                                        'sha256': hashlib.sha256(data).hexdigest()})
        self.files = files
        self.save()

    def save(self):
        atomic(self.journal, json.dumps(self.values, indent=2).encode())

    def apply(self):
        # Detect changes since planning before changing any user file.
        for path, original, _, _ in self.files:
            if read(path) != original:
                raise ValueError('配置被其他程序修改；未覆盖: ' + str(path))
        target = checked(self.values['extension'])
        self.values['phase'] = 'applying'
        self.save()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            target.rename(self.directory / 'extension-previous')
        (self.directory / 'extension-next').rename(target)
        for path, original, data, mode in self.files:
            if read(path) != original:
                raise ValueError('配置在提交时被修改: ' + str(path))
            if original != data:
                atomic(path, data, mode)

    def rollback(self):
        conflicts = []
        for item in reversed(self.values['files']):
            path = checked(item['path'])
            original = (self.directory / item['backup']).read_bytes() if item['hadFile'] else None
            current = read(path)
            if current == original:
                continue
            if current is None or hashlib.sha256(current).hexdigest() != item['sha256']:
                conflicts.append(str(path))
                continue
            if original is None:
                path.unlink()
            else:
                atomic(path, original, item['oldMode'])
        target = checked(self.values['extension'])
        previous = self.directory / 'extension-previous'
        # extension-next disappearing proves that our new directory was switched in.
        switched = not (self.directory / 'extension-next').exists() and not self.values.get('extensionRestored')
        if switched and target.exists():
            if tree_digest(target) == self.values['extensionDigest']:
                target.rename(self.directory / 'extension-failed')
            else:
                conflicts.append(str(target))
        if previous.exists() and not target.exists():
            previous.rename(target)
            self.values['extensionRestored'] = True
        elif not previous.exists() and (not switched or not target.exists()):
            self.values['extensionRestored'] = True
        self.values['phase'] = 'rollback-conflict' if conflicts else 'rolled-back'
        self.save()
        if conflicts:
            raise RuntimeError('恢复时发现外部修改，已保留备份: ' + ', '.join(conflicts))

    @classmethod
    def reopen(cls, directory):
        instance = cls.__new__(cls)
        instance.directory = checked(directory)
        instance.journal = instance.directory / 'journal.json'
        instance.values = json.loads(read(instance.journal))
        return instance


class MenuBarService:
    def __init__(self, home):
        self.domain = 'gui/' + str(os.getuid())
        self.name = self.domain + '/' + LABEL
        self.agent = home / 'Library/LaunchAgents' / (LABEL + '.plist')

    def stop(self):
        status = subprocess.run(['/bin/launchctl', 'print', self.name], capture_output=True)
        if status.returncode == 0:
            subprocess.run(['/bin/launchctl', 'bootout', self.name], check=True, capture_output=True)
        elif b'Could not find service' not in status.stderr:
            raise RuntimeError('无法读取菜单栏登录启动状态: ' + status.stderr.decode('utf-8', 'replace').strip())
        # Also handle a copy previously opened from Finder, using exact identity,
        # uid and executable checks rather than killing every helper process.
        pids = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP:47631', '-sTCP:LISTEN', '-t'],
                              capture_output=True, text=True)
        if pids.returncode not in (0, 1) or pids.stderr.strip():
            raise RuntimeError('无法核对菜单栏状态端口: ' + pids.stderr.strip())
        for text in set(pids.stdout.split()):
            pid = int(text)
            path = ctypes.create_string_buffer(4096)
            library = ctypes.CDLL('/usr/lib/libproc.dylib')
            if library.proc_pidpath(pid, path, len(path)) <= 0:
                continue
            executable = Path(os.fsdecode(path.value))
            owner = subprocess.run(['/bin/ps', '-p', text, '-o', 'uid='], capture_output=True, text=True)
            command = subprocess.run(['/bin/ps', '-p', text, '-o', 'command='], capture_output=True, text=True)
            if not owner.stdout.strip() or not command.stdout.strip():
                continue  # The old process exited between lsof and ps.
            args = command.stdout.strip()
            releases = self.agent.parents[1] / 'Application Support/workbuddy-cua/releases'
            known_arguments = (args in (str(executable), str(executable) + ' --menubar') or
                               re.fullmatch(re.escape(str(executable)) + r' -psn_[0-9_]+', args))
            if (owner.stdout.strip() != str(os.getuid()) or
                    not (executable == APP / 'Contents/MacOS/workbuddy-cua-native' or releases in executable.parents) or
                    not known_arguments):
                raise RuntimeError('状态端口被其他程序占用；未终止该进程')
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            until = time.monotonic() + 5
            while time.monotonic() < until:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError('旧菜单栏程序未退出')

    def start(self):
        subprocess.run(['/bin/launchctl', 'bootstrap', self.domain, str(self.agent)], check=True, capture_output=True)
        subprocess.run(['/bin/launchctl', 'kickstart', self.name], check=True, capture_output=True)

    def wait_ready(self, executable, info):
        until = time.monotonic() + 15
        while time.monotonic() < until:
            connection = http.client.HTTPConnection('127.0.0.1', 47631, timeout=0.5)
            try:
                connection.request('GET', '/')
                service = json.loads(connection.getresponse().read(8192)).get('_service', {})
                if (service.get('executable') == str(executable) and
                        service.get('version') == info['CFBundleShortVersionString'] and
                        service.get('build') == info['CFBundleVersion']):
                    loaded = subprocess.run(['/bin/launchctl', 'print', self.name], capture_output=True, text=True, check=True)
                    if re.search(r'\bpid = ' + str(service.get('pid')) + r'\b', loaded.stdout):
                        return
            except (OSError, ValueError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(0.2)
        raise RuntimeError('新版菜单栏未通过启动、版本和进程检查')


def verify_code(app, identifier):
    requirement = '=anchor apple generic and identifier "' + identifier + '" and certificate leaf[subject.OU] = "' + TEAM + '"'
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', '-R', requirement, str(app)], check=True, capture_output=True)


def handshake(bridge, helper):
    request = json.dumps({'id': 1, 'method': 'initialize', 'params': {}}) + '\n'
    reply = subprocess.run([sys.executable, '-B', str(bridge)], input=request, capture_output=True,
                           text=True, timeout=15, env=dict(os.environ, CUA_HELPER_PATH=str(helper)))
    if reply.returncode or json.loads(reply.stdout)['result']['serverInfo']['name'] != 'workbuddy-cua':
        raise ValueError('安装副本 MCP 握手失败；未提交用户配置')


def verify_helper(helper):
    helper = checked(helper)
    helper_app = helper.parents[2]
    if PUBLIC not in helper.parents:
        raise ValueError('helper 必须使用系统管理的独立签名版本')
    for path in (helper, *helper.parents):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('helper 路径权限不安全: ' + str(path))
    verify_code(helper_app, 'com.workbuddy.cua.helper')


def deploy(home, helper, app=APP, support=SUPPORT, service=None):
    runtime = preflight(home)
    verify_helper(helper)
    verify_code(app, LABEL)
    menu_info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    helper_info = plistlib.loads((helper.parents[2] / 'Contents/Info.plist').read_bytes())
    if any(helper_info.get(key) != menu_info.get(key) for key in ('CFBundleShortVersionString', 'CFBundleVersion')):
        raise ValueError('独立 helper 与菜单栏版本不一致；停止安装')
    extension_source = support / 'extension/extension-stage'
    digest = hashlib.sha256((tree_digest(app) + tree_digest(extension_source, EXTENSION_IGNORES)).encode()).hexdigest()[:20]
    release = checked(runtime / 'releases' / digest)
    if not release.exists():
        release.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = Path(tempfile.mkdtemp(prefix='.release-', dir=release.parent))
        try:
            shutil.copytree(app, stage / 'workbuddy-cua.app')
            shutil.copytree(extension_source, stage / 'extension', ignore=shutil.ignore_patterns(*EXTENSION_IGNORES))
            stage.rename(release)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    if (tree_digest(release / 'workbuddy-cua.app') != tree_digest(app) or
            tree_digest(release / 'extension') != tree_digest(extension_source, EXTENSION_IGNORES)):
        raise ValueError('版本目录内容不一致；停止安装')
    menu_app = release / 'workbuddy-cua.app'
    verify_code(menu_app, LABEL)
    binary = menu_app / 'Contents/MacOS/workbuddy-cua-native'
    bridge = menu_app / 'Contents/Resources/bridge/server.py'
    config, original, rendered = config_plan(home, bridge, helper)
    handshake(bridge, helper)
    extension = checked(runtime / 'extension/extension-stage')
    sys.path.insert(0, str(release / 'extension'))
    spec = importlib.util.spec_from_file_location('package_native_host', release / 'extension/install_native_host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    plan = host.plan(home=home)
    registration = Path(plan['manifestPath'])
    previous = read(registration)
    if previous is not None:
        value = json.loads(previous)
        if (not isinstance(value, dict) or value.get('name') != host.NAME or
                value.get('allowed_origins') != plan['manifest']['allowed_origins'] or
                Path(value.get('path', '')) != Path(plan['launcherPath'])):
            raise ValueError('原生宿主注册被其他组件占用；未覆盖')
    files = [(config, original, rendered, 0o600)]
    for source, target in ((release / 'extension/native_host.py', Path(plan['hostPath'])),
                           (release / 'extension/browser_events.py', Path(plan['bridgePath']))):
        files.append((target, read(target), source.read_bytes(), 0o600))
    launcher = Path(plan['launcherPath'])
    files.append((launcher, read(launcher), host.launcher_script(sys.executable, Path(plan['hostPath'])).encode(), 0o700))
    native_config = Path(plan['configPath'])
    files.append((native_config, read(native_config), json.dumps({'allowed_origins': plan['manifest']['allowed_origins']}).encode(), 0o600))
    files.append((registration, previous, json.dumps(plan['manifest']).encode(), 0o600))
    agent = checked(home / 'Library/LaunchAgents' / (LABEL + '.plist'))
    old_agent = read(agent)
    agent_data = plistlib.dumps({'Label': LABEL, 'ProgramArguments': [str(binary), '--menubar'],
                               'RunAtLoad': False, 'KeepAlive': False, 'ProcessType': 'Interactive',
                               'LimitLoadToSessionType': 'Aqua'})
    files.append((agent, old_agent, agent_data, 0o644))
    # Create the runtime state directory expected by the native host.
    checked(plan['statePath']).mkdir(parents=True, exist_ok=True, mode=0o700)
    transactions = runtime / 'transactions'
    transactions.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix=datetime.datetime.now().strftime('%Y%m%d-%H%M%S-'), dir=transactions))
    transaction = Transaction(directory, files, extension, release / 'extension')
    service = service or MenuBarService(home)
    stopped = False
    try:
        stopped = True
        service.stop()
        transaction.apply()
        transaction.values['phase'] = 'committed'
        transaction.save()
    except Exception as error:
        recovery_errors = []
        if stopped:
            try:
                service.stop()
            except Exception as failure:
                recovery_errors.append(str(failure))
        try:
            transaction.rollback()
        except Exception as failure:
            recovery_errors.append(str(failure))
        if stopped and old_agent is not None and not recovery_errors:
            try:
                service.start()
            except Exception as failure:
                recovery_errors.append(str(failure))
        raise RuntimeError(str(error) + '\n用户文件恢复结果: ' + ('；'.join(recovery_errors) or '已恢复') +
                           '\n安装记录: ' + str(transaction.journal)) from error
    return {'installed': True, 'version': plistlib.loads((menu_app / 'Contents/Info.plist').read_bytes())['CFBundleShortVersionString'],
            'helper': str(helper), 'journal': str(transaction.journal), 'startupVerified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--helper', type=Path)
    parser.add_argument('--recover-latest', action='store_true')
    args = parser.parse_args()
    home = Path.home()
    if args.check:
        preflight(home)
        print('环境与现有连接配置检查通过')
        return
    runtime = checked(home / 'Library/Application Support/workbuddy-cua')
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = checked(runtime / '.install.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.recover_latest:
            journals = [path for path in sorted((runtime / 'transactions').glob('*/journal.json'))
                        if json.loads(read(path))['phase'] not in ('committed', 'rolled-back')]
            if not journals:
                print('没有需要恢复的用户安装事务')
                return
            service = MenuBarService(home)
            service.stop()
            for journal in reversed(journals):
                Transaction.reopen(journal.parent).rollback()
            if service.agent.is_file():
                service.start()
            print('用户配置与扩展已恢复；请重新运行安装包')
        else:
            if not args.helper:
                parser.error('需要 --helper')
            print(json.dumps(deploy(home, args.helper), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('安装未完成: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
